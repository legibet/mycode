"""Command-line entrypoint for mycode."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Annotated
from uuid import uuid4

import typer

from mycode.agent import Agent
from mycode.messages import ConversationMessage, flatten_message_text
from mycode_cli import __version__
from mycode_cli.config import (
    ResolvedProvider,
    Settings,
    get_settings,
    parse_permission,
    resolve_sessions_dir,
)
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT
from mycode_cli.sessions import SessionStore
from mycode_cli.state import load_state, resolve_remembered_provider
from mycode_cli.workspace import resolve_cwd

from .runtime import build_agent

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
session_app = typer.Typer(help="Session management")
app.add_typer(session_app, name="session")


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


async def run_noninteractive(agent: Agent, message: str, *, store: SessionStore, cwd: str) -> int:
    """Run one message non-interactively and print only the final assistant reply."""

    await store.record_user_turn(agent.session_id, cwd=cwd, text=message)
    latest_assistant: ConversationMessage | None = None

    async def track(payload: ConversationMessage) -> None:
        nonlocal latest_assistant
        if payload["role"] == "assistant":
            latest_assistant = payload

    error_message = ""
    async with aclosing(agent.achat(message, on_persist=track)) as stream:
        async for event in stream:
            if event.type == "error":
                error_message = event.message or "agent error"
            elif (
                event.type == "tool_done"
                and event.is_error
                and event.output in {PERMISSION_DENIED_OUTPUT, PERMISSION_DENIED_BY_USER_OUTPUT}
            ):
                error_message = event.output

    reply = flatten_message_text(latest_assistant) if latest_assistant else ""

    if reply:
        sys.stdout.write(reply)
        if not reply.endswith("\n"):
            sys.stdout.write("\n")

    if error_message:
        if not reply:
            print(error_message, file=sys.stderr)
        return 1

    return 0


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
    reasoning_effort: str | None = None,
) -> _BootstrapContext:
    """Shared setup for the chat and run commands."""

    if session and continue_last:
        raise typer.BadParameter("--session and --continue are mutually exclusive")

    store = SessionStore(data_dir=resolve_sessions_dir())
    try:
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
    except ValueError as exc:
        from .tui.render import console, error_line

        console.print(error_line(str(exc)))
        raise SystemExit(1) from exc

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
    provider: Annotated[str | None, typer.Option(help="Provider id or configured alias")] = None,
    model: Annotated[str | None, typer.Option(help="Model name (overrides default)")] = None,
    max_turns: Annotated[int | None, typer.Option(min=1, help="Limit agent loop turns")] = None,
    permission: Annotated[
        str | None,
        typer.Option("--permission", help="Permission level: readonly, safe, standard, yolo"),
    ] = None,
    session: Annotated[str | None, typer.Option(help="Resume a specific session id")] = None,
    continue_last: Annotated[bool, typer.Option("--continue", "-c", help="Resume the most recent session")] = False,
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
    """Interactive coding agent."""

    del version  # Consumed by the eager --version callback before the body runs.

    if ctx.invoked_subcommand is not None:
        return

    from .tui.chat import TerminalChat

    setup = _bootstrap(
        provider=provider,
        model=model,
        session=session,
        continue_last=continue_last,
        permission=permission,
    )

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
    message: Annotated[list[str], typer.Argument(help="Prompt to send")],
    provider: Annotated[str | None, typer.Option(help="Provider id or configured alias")] = None,
    model: Annotated[str | None, typer.Option(help="Model name (overrides default)")] = None,
    reasoning_effort: Annotated[str | None, typer.Option("--effort", help="Reasoning effort for this run")] = None,
    max_turns: Annotated[int | None, typer.Option(min=1, help="Limit agent loop turns")] = None,
    permission: Annotated[
        str | None,
        typer.Option("--permission", help="Permission level: readonly, safe, standard, yolo"),
    ] = None,
    session: Annotated[str | None, typer.Option(help="Resume a specific session id")] = None,
    continue_last: Annotated[bool, typer.Option("--continue", "-c", help="Resume the most recent session")] = False,
) -> None:
    """Send one message and exit."""

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

    code = asyncio.run(run_noninteractive(agent, " ".join(message).strip(), store=setup.store, cwd=setup.settings.cwd))
    raise SystemExit(code)


@app.command()
def web(
    hostname: Annotated[str, typer.Option(help="Hostname to listen on")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port to listen on")] = None,
    dev: Annotated[bool, typer.Option(help="API-only backend with hot reload for web UI dev workflows")] = False,
) -> None:
    """Start the web server."""

    resolved_port = port or int(os.environ.get("PORT", "8000"))

    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    app_factory = "mycode_cli.server.app:create_web_app"
    if dev:
        app_factory = "mycode_cli.server.app:create_api_app"

    uvicorn.run(
        app_factory,
        host=hostname,
        port=resolved_port,
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
