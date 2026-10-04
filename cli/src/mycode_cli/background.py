"""Background bash jobs: one registry per session that outlives agent runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mycode import ContentBlock, ConversationMessage

logger = logging.getLogger(__name__)


@dataclass
class BackgroundJob:
    tool_use_id: str
    label: str
    pid: int
    started_at: str
    # Drains the process output and calls ``BackgroundJobs.notify`` on exit.
    task: asyncio.Task[None]
    # Kills the process group; the drain task then ends normally with the signal's exit code.
    kill: Callable[[], None]

    def info(self) -> dict[str, Any]:
        return {"tool_use_id": self.tool_use_id, "label": self.label, "pid": self.pid, "started_at": self.started_at}


class BackgroundJobs:
    """Live background jobs of one session and their undelivered results.

    A finished job's notification block stays in ``pending`` until a run has
    committed it. ``take_pending`` is the only way blocks reach a run and marks
    them in flight; ``reconcile`` with that run's ``agent.messages`` drops the
    committed blocks and releases the rest. The host sets ``deliver`` to a
    callback that checks the registry; it carries no message.
    """

    def __init__(self) -> None:
        self.deliver: Callable[[], Awaitable[None]] | None = None
        # Called with ``"job_started"`` or ``"job_finished"`` and the job, for live job lists.
        self.on_change: Callable[[str, BackgroundJob], None] | None = None
        self.jobs: list[BackgroundJob] = []
        self.pending: list[ContentBlock] = []
        self.in_flight: set[str] = set()
        # Set by a user Stop or a wake that never committed; cleared by the next user message.
        self.suspended = False

    def add(self, job: BackgroundJob) -> None:
        self.jobs.append(job)
        job.task.add_done_callback(lambda _: self._forget(job))
        self._changed("job_started", job)

    def _forget(self, job: BackgroundJob) -> None:
        if job in self.jobs:
            self.jobs.remove(job)
            self._changed("job_finished", job)

    def _changed(self, event: str, job: BackgroundJob) -> None:
        if self.on_change is not None:
            self.on_change(event, job)

    def kill(self, tool_use_id: str) -> bool:
        """Kill a live job's process; its result is still delivered, with the signal's exit code."""

        for job in self.jobs:
            if job.tool_use_id == tool_use_id:
                job.kill()
                return True
        return False

    async def notify(self, block: ContentBlock) -> None:
        self.pending.append(block)
        await self.deliver_pending()

    async def deliver_pending(self) -> None:
        """Ask the host to deliver the pending blocks; a failure leaves them pending for the next chance."""

        if not self.pending or self.deliver is None:
            return
        try:
            await self.deliver()
        except Exception:
            logger.exception("background job delivery failed")

    def deliverable(self) -> list[ContentBlock]:
        """The pending blocks not yet handed to a run."""

        return [block for block in self.pending if job_id(block) not in self.in_flight]

    def take_pending(self) -> list[ContentBlock]:
        """Take the deliverable blocks, marking them in flight."""

        blocks = self.deliverable()
        self.in_flight.update(block_id for block in blocks if (block_id := job_id(block)))
        return blocks

    def reconcile(self, messages: list[ConversationMessage]) -> None:
        """Drop the pending blocks that ``messages`` carry and release the rest."""

        committed = {
            job_id(block)
            for message in messages
            if message.get("role") == "user"
            for block in message.get("content") or []
            if job_id(block)
        }
        self.pending = [block for block in self.pending if job_id(block) not in committed]
        self.in_flight.clear()

    async def close(self) -> None:
        """Kill every live job and forget undelivered results; the registry stays usable."""

        jobs, self.jobs = self.jobs, []
        for job in jobs:
            # Killed here: a drain task that has not started yet never runs its cleanup.
            job.kill()
            job.task.cancel()
            self._changed("job_finished", job)
        await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
        self.pending.clear()
        self.in_flight.clear()


def job_id(block: ContentBlock) -> str | None:
    """Return the ``tool_use_id`` of a job notification block, or ``None`` for other blocks."""

    meta = block.get("meta")
    job = meta.get("job") if isinstance(meta, dict) else None
    return str(job["tool_use_id"]) if isinstance(job, dict) else None


def is_job_message(message: ConversationMessage) -> bool:
    """Whether every block of ``message`` is a job notification."""

    content = message.get("content") or []
    return bool(content) and all(job_id(block) for block in content)
