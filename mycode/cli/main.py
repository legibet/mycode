"""Command-line entrypoint for mycode."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated

import typer

from mycode.core.agent import Agent
from mycode.core.config import get_settings, resolve_provider
from mycode.core.session import SessionStore

from .chat import TerminalChat
from .render import TerminalView
from .runtime import resolve_session

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
session_app = typer.Typer(help="Session management")
app.add_typer(session_app, name="session")


# -- Shared helpers ----------------------------------------------------------


async def run_noninteractive(
    agent: Agent,
    *,
    store: SessionStore,
    session_id: str,
    message: str,
) -> int:
    """Run one CLI message and print only the final assistant reply."""

    latest_assistant: dict | None = None

    async def persist(payload: dict) -> None:
        nonlocal latest_assistant
        if payload.get("role") == "assistant":
            latest_assistant = payload
        await store.append_message(
            session_id,
            payload,
            provider=agent.provider,
            model=agent.model,
            cwd=agent.cwd,
            api_base=agent.api_base,
        )

    error_message = ""
    async for event in agent.achat(message, on_persist=persist):
        if event.type == "error":
            error_message = str(event.data.get("message") or "agent error")

    if error_message:
        print(error_message, file=sys.stderr)
        return 1

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
    return 0


def _build_agent(
    *,
    store: SessionStore,
    cwd: str,
    settings,
    resolved_provider,
    resolved_session,
    max_turns: int | None,
) -> Agent:
    """Build the CLI agent from the resolved provider and session state."""

    return Agent(
        model=resolved_provider.model,
        provider=resolved_provider.provider,
        cwd=cwd,
        session_dir=store.session_dir(resolved_session.session_id),
        session_id=resolved_session.session_id,
        api_key=resolved_provider.api_key,
        api_base=resolved_provider.api_base,
        messages=resolved_session.messages,
        settings=settings,
        reasoning_effort=resolved_provider.reasoning_effort,
        max_tokens=resolved_provider.max_tokens,
        context_window=resolved_provider.context_window,
        compact_threshold=settings.compact_threshold,
        max_turns=max_turns,
    )


def _resolve_and_build(
    *,
    cwd: str,
    store: SessionStore,
    provider: str | None,
    model: str | None,
    max_turns: int | None,
    session: str | None,
    continue_last: bool,
) -> tuple:
    """Resolve provider + session, build agent. Returns (agent, resolved_provider, resolved_session)."""

    settings = get_settings(cwd)
    resolved_provider = resolve_provider(settings, provider_name=provider, model=model)
    resolved_session = asyncio.run(
        resolve_session(
            store=store,
            provider=resolved_provider.provider,
            cwd=cwd,
            model=resolved_provider.model,
            api_base=resolved_provider.api_base,
            requested_session_id=session,
            continue_last=continue_last,
        )
    )
    agent = _build_agent(
        store=store,
        cwd=cwd,
        settings=settings,
        resolved_provider=resolved_provider,
        resolved_session=resolved_session,
        max_turns=max_turns,
    )
    return agent, resolved_provider, resolved_session


# -- Commands ----------------------------------------------------------------


@app.callback(invoke_without_command=True)
def chat(
    ctx: typer.Context,
    provider: Annotated[str | None, typer.Option(help="Provider id or configured alias")] = None,
    model: Annotated[str | None, typer.Option(help="Model name (overrides default)")] = None,
    max_turns: Annotated[int | None, typer.Option(min=1, help="Limit agent loop turns")] = None,
    session: Annotated[str | None, typer.Option(help="Resume a specific session id")] = None,
    continue_last: Annotated[bool, typer.Option("--continue", "-c", help="Resume the most recent session")] = False,
) -> None:
    """Interactive coding agent."""

    if ctx.invoked_subcommand is not None:
        return

    if session and continue_last:
        raise typer.BadParameter("--session and --continue are mutually exclusive")

    cwd = os.path.abspath(os.getcwd())
    store = SessionStore()
    view = TerminalView()

    try:
        agent, resolved_provider, resolved_session = _resolve_and_build(
            cwd=cwd,
            store=store,
            provider=provider,
            model=model,
            max_turns=max_turns,
            session=session,
            continue_last=continue_last,
        )
    except ValueError as exc:
        view.console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    view.print_header(
        provider=resolved_provider.provider,
        model=resolved_provider.model,
        session=resolved_session.session,
        mode=resolved_session.mode,
        message_count=len(resolved_session.messages),
        reasoning_effort=resolved_provider.reasoning_effort,
    )
    if resolved_session.mode == "resumed":
        view.print_history_preview(resolved_session.messages)

    try:
        asyncio.run(
            TerminalChat(
                agent=agent,
                store=store,
                session_id=resolved_session.session_id,
                view=view,
            ).run()
        )
    except KeyboardInterrupt:
        pass


@app.command()
def run(
    message: Annotated[list[str], typer.Argument(help="Prompt to send")],
    provider: Annotated[str | None, typer.Option(help="Provider id or configured alias")] = None,
    model: Annotated[str | None, typer.Option(help="Model name (overrides default)")] = None,
    max_turns: Annotated[int | None, typer.Option(min=1, help="Limit agent loop turns")] = None,
    session: Annotated[str | None, typer.Option(help="Resume a specific session id")] = None,
    continue_last: Annotated[bool, typer.Option("--continue", "-c", help="Resume the most recent session")] = False,
) -> None:
    """Send one message and exit."""

    if session and continue_last:
        raise typer.BadParameter("--session and --continue are mutually exclusive")

    cwd = os.path.abspath(os.getcwd())
    store = SessionStore()
    view = TerminalView()

    try:
        agent, _, resolved_session = _resolve_and_build(
            cwd=cwd,
            store=store,
            provider=provider,
            model=model,
            max_turns=max_turns,
            session=session,
            continue_last=continue_last,
        )
    except ValueError as exc:
        view.console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    code = asyncio.run(
        run_noninteractive(
            agent,
            store=store,
            session_id=resolved_session.session_id,
            message=" ".join(message).strip(),
        )
    )
    raise SystemExit(code)


@app.command()
def web(
    hostname: Annotated[str, typer.Option(help="Hostname to listen on")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port to listen on")] = None,
    dev: Annotated[bool, typer.Option(help="API-only backend for frontend dev workflows")] = False,
) -> None:
    """Start the web server."""

    cwd = os.path.abspath(os.getcwd())
    settings = get_settings(cwd)
    resolved_port = port or settings.port

    import uvicorn

    from mycode.server.app import create_app

    uvicorn.run(create_app(serve_frontend=not dev), host=hostname, port=resolved_port)


@session_app.command("list")
def session_list(
    all_workspaces: Annotated[bool, typer.Option("--all", help="Show sessions from all workspaces")] = False,
) -> None:
    """List saved sessions."""

    cwd = os.path.abspath(os.getcwd())
    store = SessionStore()
    view = TerminalView()

    sessions = asyncio.run(store.list_sessions(cwd=None if all_workspaces else cwd))
    heading = "all sessions" if all_workspaces else f"sessions for {cwd}"
    view.print_session_list(sessions, include_cwd=all_workspaces, heading=heading)


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
