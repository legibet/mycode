"""Agent construction and session-level helpers shared by the TUI and the web server."""

from __future__ import annotations

from mycode.agent import Agent
from mycode.models import estimate_cost, resolve_model_metadata
from mycode.session import SessionStore
from mycode.tools import bash_tool, edit_tool, read_tool, write_tool
from mycode_cli.config import ResolvedProvider, Settings
from mycode_cli.permissions import ToolReviewCallback, build_permission_hooks
from mycode_cli.system_prompt import build_system_prompt


async def load_session_cost(store: SessionStore, session_id: str) -> float | None:
    """Estimate the session's cumulative USD cost from its raw JSONL timeline.

    Every persisted provider request counts: tool loops, compact summaries,
    and turns discarded by rewind (billed is billed). Each assistant/compact
    record is priced by its own ``meta.provider``/``meta.model`` through the
    SDK catalog; an upstream-reported ``cost_usd`` wins. Returns None when any
    recorded request cannot be priced (no usage, unknown model) — a partial
    sum must not look like the total.
    """

    total = 0.0
    for message in await store.load_raw_messages(session_id):
        if message.get("role") not in {"assistant", "compact"}:
            continue
        meta = message.get("meta") or {}
        usage = meta.get("usage")
        if not isinstance(usage, dict):
            return None
        metadata = resolve_model_metadata(provider=str(meta.get("provider") or ""), model=str(meta.get("model") or ""))
        request_cost = estimate_cost(usage, metadata.cost)
        if request_cost is None:
            return None
        total += request_cost
    return total


def build_agent(
    *,
    store: SessionStore,
    cwd: str,
    settings: Settings,
    resolved_provider: ResolvedProvider,
    session_id: str,
    max_turns: int | None = None,
    reasoning_effort: str | None = None,
    review: ToolReviewCallback | None = None,
) -> Agent:
    """Build an agent from the resolved provider, honoring model config overrides.

    History is auto-loaded from disk when ``session_id`` already exists under
    the store; callers never pass messages explicitly.
    """

    model_config = resolved_provider.model_config
    hooks = build_permission_hooks(settings, review=review)
    return Agent(
        model=resolved_provider.model,
        provider=resolved_provider.provider,
        cwd=cwd,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=resolved_provider.api_key,
        api_base=resolved_provider.api_base,
        reasoning_effort=reasoning_effort if reasoning_effort is not None else resolved_provider.reasoning_effort,
        supports_reasoning_effort=resolved_provider.supports_reasoning_effort,
        max_tokens=model_config.max_output_tokens if model_config else None,
        context_window=model_config.context_window if model_config else None,
        supports_reasoning=model_config.supports_reasoning if model_config else None,
        supports_image_input=model_config.supports_image_input if model_config else None,
        supports_pdf_input=model_config.supports_pdf_input if model_config else None,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
        system=build_system_prompt(cwd, settings),
        tools=[read_tool, write_tool, edit_tool, bash_tool],
        hooks=hooks,
    )
