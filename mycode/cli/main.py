"""Command-line entrypoint for mycode."""

from __future__ import annotations

import argparse
import asyncio
import os

from mycode.core.agent import Agent
from mycode.core.config import get_settings, resolve_provider
from mycode.core.session import SessionStore

from .chat import TerminalChat
from .render import ReplyRenderer, TerminalView
from .runtime import resolve_session


def _parse_positive_int(value: str) -> int:
    """Parse a positive integer argument for argparse."""

    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _add_chat_options(parser: argparse.ArgumentParser) -> None:
    """Add the shared chat/session options used by CLI commands."""

    parser.add_argument("--provider", metavar="NAME", help="provider id, or a configured provider alias")
    parser.add_argument("--model", metavar="MODEL", help="Model name (overrides resolved default)")
    parser.add_argument("--max-turns", metavar="N", type=_parse_positive_int, help="Limit agent loop to N turns")
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("--session", metavar="ID", help="Resume a specific session id")
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="Resume the most recent session in the current workspace",
    )


def create_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser for the CLI."""

    parser = argparse.ArgumentParser(description="mycode CLI")
    _add_chat_options(parser)
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run one prompt and exit")
    _add_chat_options(run_parser)
    run_parser.add_argument("message", nargs="+", help="Prompt to run")

    web_parser = subparsers.add_parser("web", help="Start the web server")
    web_parser.add_argument("--hostname", default="127.0.0.1", help="Hostname to listen on")
    web_parser.add_argument("--port", type=int, help="Port to listen on")
    web_parser.add_argument(
        "--dev",
        action="store_true",
        help="Start API-only backend for frontend dev server workflows",
    )

    session_parser = subparsers.add_parser("session", help="Session management commands")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)
    list_parser = session_subparsers.add_parser("list", help="List saved sessions")
    list_parser.add_argument("--all", action="store_true", help="Show sessions from all workspaces")
    return parser


async def run_once(agent: Agent, *, store: SessionStore, session_id: str, message: str) -> int:
    """Run one prompt and return the CLI exit code."""

    async def persist(payload: dict) -> None:
        await store.append_message(session_id, payload)

    renderer = ReplyRenderer(live_mode=False)
    return await renderer.render(agent, message, on_persist=persist)


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
        max_turns=max_turns,
    )


def run_web_server(*, cwd: str, hostname: str, port: int | None, dev: bool) -> None:
    """Start the shared FastAPI app used by the web interface."""

    settings = get_settings(cwd)
    resolved_port = port or settings.port

    import uvicorn

    # Import the web app lazily so normal CLI and TUI startup does not pull in
    # server logging or other web-only side effects.
    from mycode.server.app import create_app

    uvicorn.run(create_app(serve_frontend=not dev), host=hostname, port=resolved_port)


def main() -> None:
    """Run the mycode CLI entrypoint."""

    parser = create_parser()
    args = parser.parse_args()
    cwd = os.path.abspath(os.getcwd())

    if args.command == "web":
        run_web_server(cwd=cwd, hostname=args.hostname, port=args.port, dev=args.dev)
        return

    store = SessionStore()
    view = TerminalView()

    if args.command == "session" and args.session_command == "list":
        sessions = asyncio.run(store.list_sessions(cwd=None if args.all else cwd))
        heading = "all sessions" if args.all else f"sessions for {cwd}"
        view.print_session_list(sessions, include_cwd=args.all, heading=heading)
        return

    try:
        settings = get_settings(cwd)
        resolved_provider = resolve_provider(settings, provider_name=args.provider, model=args.model)
        resolved_session = asyncio.run(
            resolve_session(
                store=store,
                provider=resolved_provider.provider,
                cwd=cwd,
                model=resolved_provider.model,
                api_base=resolved_provider.api_base,
                requested_session_id=args.session,
                continue_last=args.continue_last,
            )
        )
    except ValueError as exc:
        view.console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    agent = _build_agent(
        store=store,
        cwd=cwd,
        settings=settings,
        resolved_provider=resolved_provider,
        resolved_session=resolved_session,
        max_turns=args.max_turns,
    )

    if args.command == "run":
        code = asyncio.run(
            run_once(
                agent,
                store=store,
                session_id=resolved_session.session_id,
                message=" ".join(args.message).strip(),
            )
        )
        raise SystemExit(code)

    view.print_header(
        provider=resolved_provider.provider,
        model=resolved_provider.model,
        session=resolved_session.session,
        mode=resolved_session.mode,
        message_count=len(resolved_session.messages),
    )
    if resolved_session.mode == "resumed":
        view.print_history_preview(resolved_session.messages)

    try:
        asyncio.run(TerminalChat(agent=agent, store=store, session_id=resolved_session.session_id, view=view).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
