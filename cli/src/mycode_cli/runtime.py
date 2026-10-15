"""Agent construction and session-level helpers shared by the TUI and the web server."""

from __future__ import annotations

from mycode.agent import Agent
from mycode_cli.background import BackgroundJobs
from mycode_cli.config import ResolvedProvider, Settings
from mycode_cli.permissions import ToolReviewCallback, build_permission_hooks
from mycode_cli.sessions import SessionStore, SessionTotals, sum_session_totals
from mycode_cli.system_prompt import build_system_prompt
from mycode_cli.tools import DEFAULT_TOOLS
from mycode_cli.web_tools import build_web_tools
from mycode_cli.workspace import CliDeps


async def load_session_totals(store: SessionStore, session_id: str) -> SessionTotals:
    """Load the session's cumulative usage and USD cost from its raw JSONL timeline.

    Counts tool loops, compact summaries, and turns discarded by rewind.
    Records without usage or cost contribute nothing to that total.
    """

    return sum_session_totals(await store.load_raw_messages(session_id))


def build_agent(
    *,
    store: SessionStore,
    cwd: str,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    session_id: str,
    max_turns: int | None = None,
    review: ToolReviewCallback | None = None,
    jobs: BackgroundJobs | None = None,
) -> Agent:
    """Build an agent from the resolved provider, honoring model config overrides.

    History is auto-loaded from disk when ``session_id`` already exists under
    the store; callers never pass messages explicitly.
    """

    metadata = resolved_provider.metadata
    hooks = build_permission_hooks(settings, review=review)
    return Agent(
        model=resolved_provider.model,
        provider=resolved_provider.provider,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=resolved_provider.api_key,
        api_base=resolved_provider.api_base,
        reasoning_effort=resolved_provider.reasoning_effort,
        legacy_max_tokens=resolved_provider.legacy_max_tokens,
        max_tokens=metadata.max_output_tokens,
        context_window=metadata.context_window,
        supports_image_input=metadata.supports_image_input,
        supports_pdf_input=metadata.supports_pdf_input,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
        system=build_system_prompt(cwd, settings),
        tools=[*DEFAULT_TOOLS, *build_web_tools(settings.web)],
        hooks=hooks,
        deps=CliDeps.for_session(cwd=cwd, data_dir=store.data_dir, session_id=session_id, jobs=jobs),
    )
