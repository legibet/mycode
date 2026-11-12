"""Background jobs (bash commands and subagents): one registry per session that outlives agent runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mycode.messages import ContentBlock, ConversationMessage, TextBlock

logger = logging.getLogger(__name__)


@dataclass
class BackgroundJob:
    # Generated for each job; the provider's call id is not an identity.
    id: str
    # The call that started the job.
    tool_use_id: str
    # The tool that started the job: ``bash`` or ``agent``.
    name: str
    label: str
    started_at: str
    # Drains the job's output and calls ``BackgroundJobs.notify`` when it ends.
    task: asyncio.Task[None]
    # Stops the job (kills the process group, cancels the subagent); the drain
    # task then ends normally and still delivers the result.
    kill: Callable[[], None]
    # The bash process.
    pid: int | None = None
    # The subagent's child session id.
    session_id: str | None = None
    # Hands a message to the running subagent; ``False`` when it is finishing its turn.
    steer: Callable[[str], bool] | None = None

    def info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "id": self.id,
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "label": self.label,
            "started_at": self.started_at,
        }
        if self.pid is not None:
            info["pid"] = self.pid
        if self.session_id is not None:
            info["session_id"] = self.session_id
        return info


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
        self.pending: list[TextBlock] = []
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

    def find(self, session_id: str) -> BackgroundJob | None:
        """Return the live subagent job running the child session ``session_id``."""

        return next((job for job in self.jobs if job.session_id == session_id), None)

    def kill(self, job_id: str) -> bool:
        """Stop a live job; its result is still delivered."""

        for job in self.jobs:
            if job.id == job_id:
                job.kill()
                return True
        return False

    async def notify(self, block: TextBlock) -> None:
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

    def deliverable(self) -> list[TextBlock]:
        """The pending blocks not yet handed to a run."""

        return [block for block in self.pending if job_id(block) not in self.in_flight]

    def take_pending(self) -> list[TextBlock]:
        """Take the deliverable blocks, marking them in flight."""

        blocks = self.deliverable()
        self.in_flight.update(block_id for block in blocks if (block_id := job_id(block)))
        return blocks

    def reconcile(self, messages: list[ConversationMessage]) -> None:
        """Drop the pending blocks that ``messages`` carry and release the rest."""

        committed = {
            job_id(block)
            for message in messages
            if message["role"] == "user"
            for block in message["content"]
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
    """Return the job id of a job notification block, or ``None`` for other blocks."""

    job = block.get("meta", {}).get("job")
    return job["id"] if job else None


def is_job_message(message: ConversationMessage) -> bool:
    """Whether every block of ``message`` is a job notification."""

    content = message["content"]
    return bool(content) and all(job_id(block) for block in content)
