"""Background bash: the tool branch and the session job registry."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mycode import ContentBlock, build_message, text_block
from mycode.tools import ToolContext, ToolExecutor
from mycode_cli.background import BackgroundJob, BackgroundJobs
from mycode_cli.tools import DEFAULT_TOOLS
from mycode_cli.workspace import CliDeps


def _ctx(tmp_path: Path, jobs: BackgroundJobs | None, tool_call_id: str = "toolu_1") -> ToolContext[CliDeps]:
    deps = CliDeps(cwd=tmp_path, tool_output_dir=tmp_path / "tool-output", jobs=jobs)
    return ToolContext(executor=ToolExecutor(DEFAULT_TOOLS), deps=deps, tool_call_id=tool_call_id)


async def _wait_for_file(path: Path, content: bytes | None = None) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or (content is not None and path.read_bytes() != content):  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def _wait_for_pending(jobs: BackgroundJobs) -> None:
    async with asyncio.timeout(5):
        await asyncio.gather(*(job.task for job in jobs.jobs))


def _job_block(tool_use_id: str) -> ContentBlock:
    return text_block("done", meta={"job": {"tool_use_id": tool_use_id, "name": "bash", "label": "x", "exit_code": 0}})


class TestTool:
    async def test_refused_without_registry(self, tmp_path: Path) -> None:
        result = await _ctx(tmp_path, None).acall("bash", {"command": "echo hi", "background": True})

        assert result.is_error is True
        assert result.output == "error: background commands are not available in this mode"

    async def test_start_returns_at_once_and_delivers_result(self, tmp_path: Path) -> None:
        jobs = BackgroundJobs()
        ctx = _ctx(tmp_path, jobs)

        result = await ctx.acall("bash", {"command": "echo first; sleep 0.2; echo second; exit 3", "background": True})

        assert result.is_error is False
        assert result.output.startswith("Started in background (pid ")
        assert result.metadata is not None
        assert result.metadata["background"] is True
        log = Path(result.metadata["log"])
        assert log == tmp_path / "tool-output" / "bash-toolu_1.log"
        assert len(jobs.jobs) == 1
        assert jobs.jobs[0].pid == result.metadata["pid"]

        await _wait_for_file(log, b"first\n")
        assert not jobs.pending

        await _wait_for_pending(jobs)
        block = jobs.pending[0]
        assert block["meta"]["job"] == {
            "tool_use_id": "toolu_1",
            "name": "bash",
            "label": "echo first; sleep 0.2; echo second; exit 3",
            "exit_code": 3,
        }
        assert block["text"].startswith("Background bash finished (pid ")
        assert "exit code 3): echo first; sleep 0.2; echo second; exit 3\n" in block["text"]
        assert block["text"].endswith(f"Log: {log}\n\nfirst\nsecond")
        assert log.read_bytes() == b"first\nsecond\n"
        assert jobs.jobs == []

    async def test_close_kills_the_process_group(self, tmp_path: Path) -> None:
        jobs = BackgroundJobs()
        marker = tmp_path / "child.pid"
        ctx = _ctx(tmp_path, jobs)
        await ctx.acall("bash", {"command": f"sleep 30 & echo $! > {marker}; wait", "background": True})
        await _wait_for_file(marker)
        child_pid = int(marker.read_text())

        await jobs.close()

        assert jobs.jobs == []
        assert jobs.pending == []
        async with asyncio.timeout(5):
            while True:  # noqa: ASYNC110
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)

    async def test_kill_ends_the_command_and_still_delivers_its_result(self, tmp_path: Path) -> None:
        jobs = BackgroundJobs()
        await _ctx(tmp_path, jobs).acall("bash", {"command": "sleep 30", "background": True})

        assert jobs.kill("toolu_1") is True
        await _wait_for_pending(jobs)

        [block] = jobs.pending
        assert block["meta"]["job"]["tool_use_id"] == "toolu_1"
        assert block["meta"]["job"]["exit_code"] < 0
        assert jobs.jobs == []


class TestRegistry:
    async def test_close_kills_a_job_whose_drain_never_ran(self) -> None:
        jobs = BackgroundJobs()
        killed: list[str] = []
        started = asyncio.Event()

        async def drain() -> None:
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(drain())
        jobs.add(
            BackgroundJob(tool_use_id="a", label="x", pid=1, started_at="", task=task, kill=lambda: killed.append("a"))
        )

        await jobs.close()

        # Cancelled before its first step: the task's own cleanup never ran.
        assert not started.is_set()
        assert killed == ["a"]
        assert jobs.jobs == []

    def test_take_pending_marks_in_flight_once(self) -> None:
        jobs = BackgroundJobs()
        jobs.pending = [_job_block("a"), _job_block("b")]

        first = jobs.take_pending()
        jobs.pending.append(_job_block("c"))
        second = jobs.take_pending()

        assert [b["meta"]["job"]["tool_use_id"] for b in first] == ["a", "b"]
        assert [b["meta"]["job"]["tool_use_id"] for b in second] == ["c"]
        assert jobs.take_pending() == []
        assert len(jobs.pending) == 3

    def test_reconcile_drops_committed_and_releases_the_rest(self) -> None:
        jobs = BackgroundJobs()
        jobs.pending = [_job_block("a"), _job_block("b")]
        taken = jobs.take_pending()

        jobs.reconcile([build_message("user", [taken[0], text_block("hello")])])

        assert [b["meta"]["job"]["tool_use_id"] for b in jobs.pending] == ["b"]
        assert jobs.in_flight == set()
        assert jobs.take_pending() == jobs.pending
