"""Command-line entrypoint for mycode."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal, get_args
from uuid import uuid4

import typer

from mycode.agent import Agent
from mycode.events import ErrorEvent, ToolDoneEvent, ToolStartEvent, UsageEvent
from mycode.messages import ConversationMessage, ToolUseBlock, flatten_message_text
from mycode.usage import Cost, Usage
from mycode_cli import __version__
from mycode_cli.config import (
    PERMISSION_LEVEL_OPTIONS,
    PermissionLevel,
    ResolvedProvider,
    Settings,
    get_settings,
    parse_permission,
    resolve_cwd,
    resolve_sessions_dir,
)
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT
from mycode_cli.sessions import SessionStore
from mycode_cli.state import load_state, resolve_remembered_provider

from .runtime import build_agent

app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
session_app = typer.Typer(help="Manage saved sessions.", no_args_is_help=True)
app.add_typer(session_app, name="session")

# Options shared by the interactive session and `run`.
ProviderOption = Annotated[str | None, typer.Option(metavar="NAME", help="Provider id or configured alias")]
ModelOption = Annotated[str | None, typer.Option(metavar="NAME", help="Model name, or provider/model")]
EffortOption = Annotated[str | None, typer.Option("--effort", metavar="LEVEL", help="Reasoning effort")]
MaxTurnsOption = Annotated[int | None, typer.Option(min=1, metavar="N", help="Limit agent loop turns")]
PermissionOption = Annotated[
    PermissionLevel | None,
    typer.Option(metavar="LEVEL", help=f"Tool permission level: {', '.join(PERMISSION_LEVEL_OPTIONS)}"),
]
SessionOption = Annotated[str | None, typer.Option(metavar="ID", help="Resume a specific session")]
ContinueOption = Annotated[bool, typer.Option("--continue", "-c", help="Resume the most recent session")]

OutputFormat = Literal["text", "json", "jsonl"]


async def resolve_session_id(
    *,
    store: SessionStore,
    cwd: str,
    requested_session_id: str | None,
    continue_last: bool,
) -> str:
    """Return the session to open: the requested one, the latest in ``cwd``, or a new id."""

    if requested_session_id:
        if await store.load_metadata(requested_session_id) is None:
            raise ValueError(f"Unknown session: {requested_session_id}")
        return requested_session_id
    if continue_last and (latest := await store.latest_session(cwd=cwd)):
        return latest["id"]
    # The catalog and timeline are both created lazily on the first user turn.
    return uuid4().hex


def _show_version(value: bool) -> None:
    if not value:
        return
    typer.echo(f"mycode {__version__}")
    raise typer.Exit()


def _write_json(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result_record(
    *,
    error: str | None,
    reply: str = "",
    session_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    duration_ms: int = 0,
    usage: Usage | None = None,
    cost: Cost | None = None,
    permission_denials: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the closing ``result`` record of the json and jsonl formats; see docs/cli.md."""

    return {
        "type": "result",
        "is_error": error is not None,
        "result": reply,
        "error": error,
        "session_id": session_id,
        "provider": provider,
        "model": model,
        "duration_ms": duration_ms,
        "usage": usage or {},
        "cost": cost,
        "permission_denials": permission_denials or [],
    }


async def run_noninteractive(
    agent: Agent,
    message: str,
    *,
    store: SessionStore,
    settings: Settings,
    provider_name: str,
    output_format: OutputFormat = "text",
) -> int:
    """Run one message non-interactively and print its outcome in ``output_format``.

    ``text`` prints only the final assistant reply; ``json`` prints the ``result``
    record; ``jsonl`` streams an ``init`` record, the agent events, then ``result``.
    """

    started = time.monotonic()
    if output_format == "jsonl":
        _write_json(
            {
                "type": "init",
                "session_id": agent.session_id,
                "cwd": settings.cwd,
                "provider": provider_name,
                "model": agent.model,
                "effort": agent.reasoning_effort,
                "permission": settings.permission.level,
                "version": __version__,
            }
        )

    latest_assistant: ConversationMessage | None = None

    async def track(payload: ConversationMessage) -> None:
        nonlocal latest_assistant
        if payload["role"] == "assistant":
            latest_assistant = payload

    error_message: str | None = None
    tool_calls: dict[str, ToolUseBlock] = {}
    permission_denials: list[dict[str, Any]] = []
    usage: Usage | None = None
    cost: Cost | None = None
    try:
        await store.record_user_turn(agent.session_id, cwd=settings.cwd, text=message)
        async with aclosing(agent.achat(message, on_persist=track)) as stream:
            async for event in stream:
                match event:
                    case ErrorEvent():
                        error_message = event.message or "agent error"
                    case ToolStartEvent():
                        tool_calls[event.tool_call["id"]] = event.tool_call
                    case ToolDoneEvent(is_error=True) if event.output in {
                        PERMISSION_DENIED_OUTPUT,
                        PERMISSION_DENIED_BY_USER_OUTPUT,
                    }:
                        error_message = event.output
                        call = tool_calls[event.tool_use_id]
                        permission_denials.append(
                            {"tool_use_id": call["id"], "name": call["name"], "input": call["input"]}
                        )
                    case UsageEvent():
                        usage, cost = event.turn_usage, event.turn_cost
                if output_format == "jsonl":
                    record = event.to_dict()
                    # Multimodal tool content (e.g. base64 images) stays in the session, as over SSE.
                    record.pop("content", None)
                    _write_json(record)
    except Exception as exc:
        # The SDK raises session persistence failures instead of yielding an error event;
        # report them like any run error so json and jsonl output still end with a result.
        error_message = str(exc) or type(exc).__name__

    reply = flatten_message_text(latest_assistant) if latest_assistant else ""

    if output_format == "text":
        if reply:
            sys.stdout.write(reply)
            if not reply.endswith("\n"):
                sys.stdout.write("\n")
        if error_message and not reply:
            print(error_message, file=sys.stderr)
    else:
        _write_json(
            _result_record(
                error=error_message,
                reply=reply,
                session_id=agent.session_id,
                provider=provider_name,
                model=agent.model,
                duration_ms=round((time.monotonic() - started) * 1000),
                usage=usage,
                cost=cost,
                permission_denials=permission_denials,
            )
        )

    return 1 if error_message else 0


@dataclass
class _BootstrapContext:
    store: SessionStore
    settings: Settings
    resolved_provider: ResolvedProvider
    session_id: str


def _bootstrap(
    *,
    provider: str | None,
    model: str | None,
    session: str | None,
    continue_last: bool,
    permission: str | None,
    reasoning_effort: str | None,
) -> _BootstrapContext:
    """Shared setup for the chat and run commands; raises ``ValueError`` for bad settings or input."""

    if session and continue_last:
        raise typer.BadParameter("--session and --continue are mutually exclusive")

    store = SessionStore(data_dir=resolve_sessions_dir())
    settings = get_settings()
    if permission is not None:
        settings = replace(settings, permission=parse_permission(permission, settings.permission))
    state = load_state()
    resolved_provider = resolve_remembered_provider(settings, state, provider_name=provider, model=model)
    if reasoning_effort is None:
        reasoning_effort = state.effort_for(resolved_provider)
    resolved_provider = resolved_provider.with_reasoning_effort(reasoning_effort)
    session_id = asyncio.run(
        resolve_session_id(store=store, cwd=settings.cwd, requested_session_id=session, continue_last=continue_last)
    )
    return _BootstrapContext(
        store=store,
        settings=settings,
        resolved_provider=resolved_provider,
        session_id=session_id,
    )


# -- Commands ----------------------------------------------------------------


@app.callback(invoke_without_command=True)
def chat(
    ctx: typer.Context,
    provider: ProviderOption = None,
    model: ModelOption = None,
    reasoning_effort: EffortOption = None,
    max_turns: MaxTurnsOption = None,
    permission: PermissionOption = None,
    session: SessionOption = None,
    continue_last: ContinueOption = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_show_version,
            help="Show version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Minimal coding agent. Without a command, start an interactive session."""

    del version  # Consumed by the eager --version callback before the body runs.

    if ctx.invoked_subcommand is not None:
        # These options configure the interactive session only; silently dropping them would
        # run a subcommand with settings the user did not ask for.
        command = ctx.invoked_subcommand
        if continue_last or any(
            v is not None for v in (provider, model, reasoning_effort, max_turns, permission, session)
        ):
            ctx.fail(
                f"Options before '{command}' only apply to the interactive session; put command options after '{command}'."
            )
        return

    from .tui.chat import TerminalChat
    from .tui.render import console, error_line

    try:
        setup = _bootstrap(
            provider=provider,
            model=model,
            session=session,
            continue_last=continue_last,
            permission=permission,
            reasoning_effort=reasoning_effort,
        )
    except ValueError as exc:
        console.print(error_line(str(exc)))
        raise SystemExit(1) from exc

    terminal_chat = TerminalChat(
        settings=setup.settings,
        store=setup.store,
        resolved_provider=setup.resolved_provider,
        session_id=setup.session_id,
        max_turns=max_turns,
    )
    asyncio.run(terminal_chat.run())


@app.command()
def run(
    prompt: Annotated[
        list[str] | None, typer.Argument(metavar="PROMPT", help="Prompt to send", show_default=False)
    ] = None,
    provider: ProviderOption = None,
    model: ModelOption = None,
    reasoning_effort: EffortOption = None,
    max_turns: MaxTurnsOption = None,
    permission: PermissionOption = None,
    session: SessionOption = None,
    continue_last: ContinueOption = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", metavar="FORMAT", help=f"Output format: {', '.join(get_args(OutputFormat))}"),
    ] = "text",
) -> None:
    """Send one prompt, print the final reply, and exit.

    Piped stdin is appended to the prompt, or used as the prompt when none is given.
    """

    message = " ".join(prompt or []).strip()
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        message = f"{message}\n\n{piped}" if message and piped else message or piped
    if not message:
        raise typer.BadParameter("provide a prompt as an argument or on stdin", param_hint="PROMPT")

    try:
        setup = _bootstrap(
            provider=provider,
            model=model,
            session=session,
            continue_last=continue_last,
            permission=permission,
            reasoning_effort=reasoning_effort,
        )
        agent = build_agent(
            store=setup.store,
            settings=setup.settings,
            resolved_provider=setup.resolved_provider,
            session_id=setup.session_id,
            max_turns=max_turns,
        )
    except (OSError, ValueError) as exc:
        if output_format == "text":
            print(exc, file=sys.stderr)
        else:
            _write_json(_result_record(error=str(exc)))
        raise SystemExit(1) from exc

    code = asyncio.run(
        run_noninteractive(
            agent,
            message,
            store=setup.store,
            settings=setup.settings,
            provider_name=setup.resolved_provider.provider_name,
            output_format=output_format,
        )
    )
    raise SystemExit(code)


@app.command()
def web(
    host: Annotated[str, typer.Option(help="Address to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="PORT", help="Port to listen on")] = 8000,
    dev: Annotated[
        bool, typer.Option("--dev", help="Serve the API only, with hot reload, for web UI development")
    ] = False,
) -> None:
    """Start the web UI server."""

    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    app_factory = "mycode_cli.server.app:create_web_app"
    if dev:
        app_factory = "mycode_cli.server.app:create_api_app"

    uvicorn.run(
        app_factory,
        host=host,
        port=port,
        reload=dev,
        factory=True,
        timeout_graceful_shutdown=5,
        log_config={
            **LOGGING_CONFIG,
            "root": {"handlers": ["default"], "level": "INFO"},
        },
    )


@session_app.command("list")
def session_list(
    all_workspaces: Annotated[bool, typer.Option("--all", help="Show sessions from all workspaces")] = False,
) -> None:
    """List saved sessions."""

    from .tui.render import console, session_list_table

    cwd = resolve_cwd(os.getcwd())
    store = SessionStore(data_dir=resolve_sessions_dir())

    sessions = asyncio.run(store.list_sessions(cwd=None if all_workspaces else cwd))
    heading = "all sessions" if all_workspaces else f"sessions for {cwd}"
    for renderable in session_list_table(sessions, include_cwd=all_workspaces, heading=heading):
        console.print(renderable)


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
