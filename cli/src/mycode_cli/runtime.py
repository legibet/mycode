"""Build an Agent from CLI settings — shared by both the TUI and the web server."""

from __future__ import annotations

from mycode.agent import Agent
from mycode.session import SessionStore
from mycode.tools import DEFAULT_TOOL_SPECS
from mycode_cli.config import ResolvedProvider, Settings
from mycode_cli.permissions import ToolReviewCallback, build_permission_hooks
from mycode_cli.system_prompt import build_system_prompt


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
        max_tokens=model_config.max_output_tokens if model_config else None,
        context_window=model_config.context_window if model_config else None,
        supports_reasoning=model_config.supports_reasoning if model_config else None,
        supports_image_input=model_config.supports_image_input if model_config else None,
        supports_pdf_input=model_config.supports_pdf_input if model_config else None,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
        system=build_system_prompt(cwd, settings),
        tools=DEFAULT_TOOL_SPECS,
        hooks=hooks,
    )
