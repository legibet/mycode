"""Command-line entrypoint for mycode."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal
from uuid import uuid4

import typer

from mycode.agent import Agent
from mycode.messages import ConversationMessage
from mycode.session import SessionStore
from mycode_cli import __version__
from mycode_cli.config import (
    ResolvedProvider,
    Settings,
    get_settings,
    parse_permission,
    resolve_provider,
    resolve_sessions_dir,
)
from mycode_cli.permissions import PERMISSION_DENIED_BY_USER_OUTPUT, PERMISSION_DENIED_OUTPUT

from .runtime import build_agent
from .tui.chat import TerminalChat
from .tui.render import TerminalView

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
session_app = typer.Typer(help="Session management")
app.add_typer(session_app, name="session")


@dataclass
class ResolvedSession:
    """The session selected for the current CLI run.

    `mode` is either `"new"` or `"resumed"`.
    """

    session_id: str
    session: dict[str, Any]
    messages: list[ConversationMessage]
    mode: Literal["new", "resumed"]


async def resolve_session(
    *,
    store: SessionStore,
    cwd: str,
    requested_session_id: str | None,
    continue_last: bool,
) -> ResolvedSession:
    """Resolve which session the CLI should load before starting."""

    if requested_session_id:
        data = await store.load_session(requested_session_id)
        if data is None:
            raise ValueError(f"Unknown session: {requested_session_id}")
        return ResolvedSession(
            requested_session_id,
            data["session"],
            data["messages"],
            "resumed",
        )

    if continue_last:
        latest = await store.latest_session(cwd=cwd)
        if latest:
            session_id = str(latest["id"])
            data = await store.load_session(session_id)
            if data is None:
                raise ValueError(f"Unknown session: {session_id}")
            return ResolvedSession(
                session_id,
                data["session"],
                data["messages"],
                "resumed",
            )

    # New sessions: the id is allocated here; the on-disk session is created
    # lazily by Agent.achat on the first persist.
    return ResolvedSession(uuid4().hex, {}, [], "new")


def _show_version(value: bool) -> None:
    if not value:
        return
    typer.echo(f"mycode {__version__}")
    raise typer.Exit()


async def run_noninteractive(agent: Agent, message: str) -> int:
    """Run one message non-interactively and print only the final assistant reply."""

    latest_assistant: ConversationMessage | None = None

    async def track(payload: ConversationMessage) -> None:
        nonlocal latest_assistant
        if payload.get("role") == "assistant":
            latest_assistant = payload

    error_message = ""
    async for event in agent.achat(message, on_persist=track):
        if event.type == "error":
            error_message = str(event.data.get("message") or "agent error")
        elif event.type == "tool_done" and event.data.get("is_error"):
            output = str(event.data.get("output") or "")
            if output in {PERMISSION_DENIED_OUTPUT, PERMISSION_DENIED_BY_USER_OUTPUT}:
                error_message = output

    reply = ""
    if latest_assistant:
        reply = "".join(
            str(block.get("text") or "")
            for block in latest_assistant.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )

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
    view: TerminalView
    settings: Settings
    resolved_provider: ResolvedProvider
    resolved_session: ResolvedSession
    agent: Agent


def _bootstrap(
    *,
    provider: str | None,
    model: str | None,
    session: str | None,
    continue_last: bool,
    max_turns: int | None,
    permission: str | None,
) -> _BootstrapContext:
    """Shared setup for the chat and run commands."""

    if session and continue_last:
        raise typer.BadParameter("--session and --continue are mutually exclusive")

    cwd = os.path.abspath(os.getcwd())
    store = SessionStore(data_dir=resolve_sessions_dir())
    view = TerminalView()
    try:
        settings = get_settings(cwd)
        if permission is not None:
            settings = replace(settings, permission=parse_permission(permission, settings.permission))
        resolved_provider = resolve_provider(settings, provider_name=provider, model=model)
        resolved_session = asyncio.run(
            resolve_session(
                store=store,
                cwd=cwd,
                requested_session_id=session,
                continue_last=continue_last,
            )
        )
    except ValueError as exc:
        view.console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    agent = build_agent(
        store=store,
        cwd=cwd,
        settings=settings,
        resolved_provider=resolved_provider,
        session_id=resolved_session.session_id,
        max_turns=max_turns,
    )

    return _BootstrapContext(
        store=store,
        view=view,
        settings=settings,
        resolved_provider=resolved_provider,
        resolved_session=resolved_session,
        agent=agent,
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

    setup = _bootstrap(
        provider=provider,
        model=model,
        session=session,
        continue_last=continue_last,
        max_turns=max_turns,
        permission=permission,
    )

    setup.view.print_header(
        provider=setup.resolved_provider.provider,
        model=setup.resolved_provider.model,
        session=setup.resolved_session.session,
        mode=setup.resolved_session.mode,
        message_count=len(setup.resolved_session.messages),
        reasoning_effort=setup.resolved_provider.reasoning_effort,
    )
    if setup.resolved_session.mode == "resumed":
        setup.view.print_history_preview(setup.resolved_session.messages)

    with suppress(KeyboardInterrupt):
        asyncio.run(
            TerminalChat(
                agent=setup.agent,
                settings=setup.settings,
                store=setup.store,
                session_id=setup.resolved_session.session_id,
                reasoning_efforts=setup.resolved_provider.reasoning_efforts,
                view=setup.view,
            ).run()
        )


@app.command()
def run(
    message: Annotated[list[str], typer.Argument(help="Prompt to send")],
    provider: Annotated[str | None, typer.Option(help="Provider id or configured alias")] = None,
    model: Annotated[str | None, typer.Option(help="Model name (overrides default)")] = None,
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
        max_turns=max_turns,
        permission=permission,
    )

    code = asyncio.run(run_noninteractive(setup.agent, " ".join(message).strip()))
    raise SystemExit(code)


@app.command()
def web(
    hostname: Annotated[str, typer.Option(help="Hostname to listen on")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port to listen on")] = None,
    dev: Annotated[bool, typer.Option(help="API-only backend with hot reload for web UI dev workflows")] = False,
) -> None:
    """Start the web server."""

    cwd = os.path.abspath(os.getcwd())
    settings = get_settings(cwd)
    resolved_port = port or settings.port

    import uvicorn

    app_factory = "mycode_cli.server.app:create_web_app"
    if dev:
        app_factory = "mycode_cli.server.app:create_api_app"

    uvicorn.run(
        app_factory,
        host=hostname,
        port=resolved_port,
        reload=dev,
        factory=True,
    )


@session_app.command("list")
def session_list(
    all_workspaces: Annotated[bool, typer.Option("--all", help="Show sessions from all workspaces")] = False,
) -> None:
    """List saved sessions."""

    cwd = os.path.abspath(os.getcwd())
    store = SessionStore(data_dir=resolve_sessions_dir())
    view = TerminalView()

    sessions = asyncio.run(store.list_sessions(cwd=None if all_workspaces else cwd))
    heading = "all sessions" if all_workspaces else f"sessions for {cwd}"
    view.print_session_list(sessions, include_cwd=all_workspaces, heading=heading)


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
