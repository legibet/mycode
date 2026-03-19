"""CLI entrypoint for mycode."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from mycode.core.agent import Agent
from mycode.core.config import Settings, get_settings, resolve_mycode_home, resolve_provider
from mycode.core.provider_registry import list_supported_providers, provider_default_models
from mycode.core.session import SessionStore
from mycode.server.app import create_app, frontend_dist_path

console = Console(highlight=False)

_PROMPT = ANSI("\033[1m\033[34m❯\033[0m ")
_MAX_WIDTH = 88


@dataclass
class CLIResolvedSession:
    session_id: str
    session: dict[str, Any]
    messages: list[dict[str, Any]]
    mode: str


@dataclass(frozen=True)
class ProviderOption:
    name: str
    provider: str
    models: tuple[str, ...]
    api_base: str | None


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _build_chat_parent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--provider",
        metavar="NAME",
        help="provider id, or a configured provider alias",
    )
    parser.add_argument("--model", metavar="MODEL", help="Model name (overrides resolved default)")
    parser.add_argument("--max-turns", metavar="N", type=_positive_int, help="Limit agent loop to N turns")
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("--session", metavar="ID", help="Resume a specific session id")
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="Resume the most recent session in the current workspace",
    )
    return parser


def _build_parser() -> argparse.ArgumentParser:
    chat_parent = _build_chat_parent_parser()
    parser = argparse.ArgumentParser(description="mycode CLI", parents=[chat_parent])
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run one prompt and exit", parents=[chat_parent])
    run_parser.add_argument("message", nargs="+", help="Prompt to run")

    web_parser = subparsers.add_parser("web", help="Start the web server")
    web_parser.add_argument("--hostname", default="127.0.0.1", help="Hostname to listen on")
    web_parser.add_argument("--port", type=int, help="Port to listen on")

    session_parser = subparsers.add_parser("session", help="Session management commands")
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)
    list_parser = session_subparsers.add_parser("list", help="List saved sessions")
    list_parser.add_argument("--all", action="store_true", help="Show sessions from all workspaces")
    return parser


def _history_file_path() -> str:
    path = resolve_mycode_home() / "cli_history"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _compact_text(value: str, *, limit: int = 96) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_timestamp(value: str) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def _provider_options(settings: Settings) -> list[ProviderOption]:
    options: list[ProviderOption] = []
    seen: set[str] = set()

    for name, config in settings.providers.items():
        raw_models = config.models or list(provider_default_models(config.type))
        models = tuple(dict.fromkeys(model.strip() for model in raw_models if model.strip()))
        options.append(ProviderOption(name=name, provider=config.type, models=models, api_base=config.base_url))
        seen.add(name)

    for provider_name in list_supported_providers():
        if provider_name in seen:
            continue
        options.append(
            ProviderOption(
                name=provider_name, provider=provider_name, models=provider_default_models(provider_name), api_base=None
            )
        )

    return options


def _find_provider_option(settings: Settings, *, provider: str, api_base: str | None) -> ProviderOption | None:
    for option in _provider_options(settings):
        if option.provider == provider and option.api_base == api_base:
            return option
    return None


def _model_options(settings: Settings, *, provider: str, api_base: str | None, current_model: str) -> list[str]:
    current = _find_provider_option(settings, provider=provider, api_base=api_base)
    if current:
        return list(dict.fromkeys([current_model, *current.models]))
    return list(dict.fromkeys([current_model, *provider_default_models(provider)]))


def _select_list_item(choice: str, options: list[str], *, label: str) -> str | None:
    value = choice.strip()
    if not value:
        return None

    if value.isdigit():
        idx = int(value) - 1
        if 0 <= idx < len(options):
            return options[idx]
        return None

    lowered = value.lower()
    exact = [option for option in options if option.lower() == lowered]
    if len(exact) == 1:
        return exact[0]

    matches = [option for option in options if option.lower().startswith(lowered)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous {label}: {value}")
    return None


def _history_preview_entries(messages: list[dict[str, Any]], *, limit: int = 6) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, list):
                text = " ".join(
                    str(block.get("text") or "").strip() for block in content if block.get("type") == "text"
                )
            else:
                text = str(content or "")
            text = _compact_text(text)
            if text:
                entries.append(("You", text))
            continue

        if role != "assistant":
            continue

        text = ""
        thinking = ""
        tool_names: list[str] = []
        if isinstance(content, list):
            text = " ".join(str(block.get("text") or "").strip() for block in content if block.get("type") == "text")
            thinking = " ".join(
                str(block.get("text") or "").strip() for block in content if block.get("type") == "thinking"
            )
            tool_names = [str(block.get("name") or "tool") for block in content if block.get("type") == "tool_use"]
        else:
            text = str(content or "")

        text = _compact_text(text)
        thinking = _compact_text(thinking)

        if text:
            if tool_names:
                text = f"{text}  [{len(tool_names)} tool{'s' if len(tool_names) != 1 else ''}]"
            entries.append(("Assistant", text))
            continue

        if thinking:
            summary = f"Thinking: {thinking}"
            if tool_names:
                summary = f"{summary}  [{len(tool_names)} tool{'s' if len(tool_names) != 1 else ''}]"
            entries.append(("Assistant", summary))
            continue

        if tool_names:
            preview = ", ".join(tool_names[:3])
            if len(tool_names) > 3:
                preview += f" +{len(tool_names) - 3}"
            entries.append(("Assistant", f"[Used tools: {preview}]"))

    if limit <= 0:
        return entries
    return entries[-limit:]


def _resolve_session_choice(choice: str, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    value = choice.strip()
    if not value:
        return None

    if value.isdigit():
        idx = int(value) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
        return None

    matches = [session for session in sessions if str(session.get("id") or "").startswith(value)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous session id: {value}")
    return None


def print_session_list(
    sessions: list[dict[str, Any]],
    *,
    include_cwd: bool = False,
    current_session_id: str | None = None,
    heading: str = "sessions",
) -> None:
    if not sessions:
        console.print("[dim]no sessions found[/dim]")
        return

    console.print(f"[dim]{heading} ({len(sessions)})[/dim]")
    for index, session in enumerate(sessions, start=1):
        parts: list[str] = []
        session_id = str(session.get("id") or "-")
        is_current = bool(current_session_id and session_id == current_session_id)
        title_limit = 24 if include_cwd else 40
        model_limit = 18 if include_cwd else 24
        cwd_limit = 32 if include_cwd else 48

        marker = "*" if is_current else " "
        parts.append(f"{marker}{index:>2}")
        parts.append(session_id[:12])
        parts.append(_format_timestamp(str(session.get("updated_at") or "")))
        parts.append(_compact_text(str(session.get("title") or "New chat"), limit=title_limit))

        model = str(session.get("model") or "")
        if model:
            parts.append(f"[{_compact_text(model, limit=model_limit)}]")

        if include_cwd:
            cwd = str(session.get("cwd") or "")
            if cwd:
                parts.append(_compact_text(cwd, limit=cwd_limit))

        console.print("  ".join(parts))


def print_header(*, provider: str, model: str, session: dict[str, Any], mode: str, message_count: int) -> None:
    console.print()
    title = session.get("title") or "New chat"
    session_id = str(session.get("id") or "")[:12]

    title_text = Text()
    title_text.append("mycode", style="bold")
    title_text.append(" ── ", style="dim")
    title_text.append(provider, style="cyan")
    title_text.append(" / ", style="dim")
    title_text.append(model)
    console.print(title_text)

    meta_text = Text()
    meta_text.append("session ", style="dim")
    meta_text.append(session_id or "-", style="bold")
    meta_text.append("  ")
    meta_text.append(mode, style="green" if mode == "new" else "yellow")
    meta_text.append("  ")
    meta_text.append(title)
    if message_count:
        meta_text.append(f"  ({message_count} stored messages)", style="dim")
    console.print(meta_text)


def print_history_preview(messages: list[dict[str, Any]]) -> None:
    entries = _history_preview_entries(messages)
    if not entries:
        return

    console.print(f"[dim]history preview (showing last {len(entries)})[/dim]")
    for role, content in entries:
        label = "user" if role == "You" else "assistant"
        console.print(f"[dim]{label}[/dim] {content}")


async def _prompt_selection(prompt_session: PromptSession, prompt_text: str) -> str:
    try:
        return await asyncio.get_event_loop().run_in_executor(None, lambda: prompt_session.prompt(ANSI(prompt_text)))
    except KeyboardInterrupt:
        return ""
    except EOFError:
        return ""


async def _switch_agent_runtime(
    agent: Agent,
    *,
    store: SessionStore,
    session_id: str,
    provider_name: str | None,
    model: str | None,
) -> bool:
    settings = get_settings(agent.cwd)
    resolved = resolve_provider(settings, provider_name=provider_name, model=model)

    changed = (
        agent.provider != resolved.provider
        or agent.model != resolved.model
        or agent.api_base != resolved.api_base
        or agent.api_key != resolved.api_key
        or agent.reasoning_effort != resolved.reasoning_effort
        or agent.max_tokens != resolved.max_tokens
    )

    await store.get_or_create(
        session_id,
        provider=resolved.provider,
        model=resolved.model,
        cwd=agent.cwd,
        api_base=resolved.api_base,
    )

    # Provider/model switching only changes request settings.
    # The current conversation and tool executor stay with the same agent.
    agent.provider = resolved.provider
    agent.model = resolved.model
    agent.api_key = resolved.api_key
    agent.api_base = resolved.api_base
    agent.reasoning_effort = resolved.reasoning_effort
    agent.max_tokens = resolved.max_tokens
    agent.settings = settings

    return changed


async def resolve_cli_session(
    *,
    store: SessionStore,
    provider: str,
    cwd: str,
    model: str,
    api_base: str | None,
    requested_session_id: str | None,
    continue_last: bool,
) -> CLIResolvedSession:
    """Resolve the session the CLI should start with."""

    if requested_session_id:
        data = await store.load_session(requested_session_id)
        if not data or not data.get("session"):
            raise ValueError(f"Unknown session: {requested_session_id}")

        synced = await store.get_or_create(
            requested_session_id,
            provider=provider,
            model=model,
            cwd=cwd,
            api_base=api_base,
        )
        session = synced.get("session") or data["session"]
        messages = synced.get("messages") or data.get("messages") or []
        return CLIResolvedSession(
            session_id=requested_session_id,
            session=session,
            messages=messages,
            mode="resumed",
        )

    if continue_last:
        latest = await store.latest_session(cwd=cwd)
        if latest and latest.get("id"):
            session_id = str(latest["id"])
            data = await store.get_or_create(
                session_id,
                provider=provider,
                model=model,
                cwd=cwd,
                api_base=api_base,
            )
            return CLIResolvedSession(
                session_id=session_id,
                session=data.get("session") or latest,
                messages=data.get("messages") or [],
                mode="resumed",
            )

    data = await store.create_session(None, provider=provider, model=model, cwd=cwd, api_base=api_base)
    session = data.get("session") or {}
    return CLIResolvedSession(
        session_id=str(session.get("id") or ""),
        session=session,
        messages=[],
        mode="new",
    )


class TUIRenderer:
    """Render streaming agent output for interactive and one-shot CLI modes."""

    def __init__(self, con: Console, *, live_mode: bool = True):
        self._con = con
        self._live_mode = live_mode
        self._live: Live | None = None
        self._reasoning: list[str] = []
        self._text: list[str] = []
        self._printed_static_reasoning = False

    @staticmethod
    def _tool_preview(args: dict) -> str:
        if not args:
            return ""
        value = str(next(iter(args.values())))
        return value[:60] + "…" if len(value) > 60 else value

    @staticmethod
    def _result_preview(result: str) -> str:
        lines = result.splitlines()
        if not lines:
            return ""
        first = lines[0][:72]
        if len(lines) > 1:
            first += f"  (+{len(lines) - 1} lines)"
        elif len(lines[0]) > 72:
            first += "…"
        return first

    def start(self) -> None:
        if self._live_mode:
            self._ensure_live()

    def reasoning(self, chunk: str) -> None:
        self._reasoning.append(chunk)
        if self._live_mode:
            self._ensure_live()
            self._update()

    def text(self, chunk: str) -> None:
        self._print_static_reasoning()
        if self._live_mode:
            self._text.append(chunk)
            self._ensure_live()
            self._update()
        elif chunk:
            self._con.print(chunk, end="", markup=False, highlight=False)

    def tool_start(self, name: str, args: dict) -> None:
        self._print_static_reasoning()
        self._flush()
        if not self._live_mode:
            self._con.print()
        preview = self._tool_preview(args)
        text = Text()
        text.append("⏺ ", style="green")
        text.append(name.capitalize(), style="bold green")
        if preview:
            text.append(f" {preview}", style="dim")
        self._con.print(text)

    def tool_output(self, line: str) -> None:
        if line:
            text = Text("  │ ", style="dim")
            text.append(line, style="dim")
            self._con.print(text)

    def tool_done(self, result: str) -> None:
        preview = self._result_preview(result)
        style = "red" if result.startswith("error") else "dim"
        text = Text("  ⎿ ", style=style)
        text.append(preview, style=style)
        self._con.print(text)

    def error(self, message: str) -> None:
        self._print_static_reasoning()
        self._flush()
        text = Text("✕ ", style="red")
        text.append(message, style="red")
        self._con.print(text)

    def finish(self) -> None:
        self._print_static_reasoning()
        self._flush()
        if not self._live_mode:
            self._con.print()

    def cancel(self) -> None:
        self._print_static_reasoning()
        self._flush()
        self._con.print("[dim]cancelled[/dim]")

    def _ensure_live(self) -> None:
        if self._live is None:
            self._live = Live(self._renderable(), console=self._con, refresh_per_second=12)
            self._live.start()

    def _update(self) -> None:
        if self._live is not None:
            self._live.update(self._renderable())

    def _flush(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._reasoning.clear()
        self._text.clear()
        self._printed_static_reasoning = False

    def _print_static_reasoning(self) -> None:
        if self._live_mode or self._printed_static_reasoning or not self._reasoning:
            return
        self._con.print("Thinking", style="dim bold")
        self._con.print("".join(self._reasoning), style="dim")
        self._printed_static_reasoning = True

    def _renderable(self):
        if not self._reasoning and not self._text:
            return Spinner("dots", style="dim")
        parts = []
        if self._reasoning:
            reasoning = Text()
            reasoning.append("Thinking\n", style="dim bold")
            reasoning.append("".join(self._reasoning), style="dim")
            parts.append(reasoning)
        if self._text:
            parts.append(Markdown("".join(self._text)))
        return Group(*parts) if len(parts) > 1 else parts[0]


async def run_once(agent: Agent, *, store: SessionStore, session_id: str, message: str) -> int:
    async def on_persist(payload: dict) -> None:
        await store.append_message(session_id, payload)

    renderer = TUIRenderer(console, live_mode=False)
    exit_code = 0

    async for event in agent.achat(message, on_persist=on_persist):
        match event.type:
            case "reasoning":
                renderer.reasoning(event.data.get("content", ""))
            case "text":
                renderer.text(event.data.get("content", ""))
            case "tool_start":
                renderer.tool_start(event.data.get("name", ""), event.data.get("args") or {})
            case "tool_output":
                renderer.tool_output(event.data.get("content", ""))
            case "tool_done":
                result = event.data.get("result", "")
                renderer.tool_done(result)
                if result.startswith("error"):
                    exit_code = 1
            case "error":
                exit_code = 1
                renderer.error(event.data.get("message", ""))

    renderer.finish()
    return exit_code


async def chat_loop(agent: Agent, *, store: SessionStore, session_id: str) -> None:
    """Run the interactive terminal loop for a single workspace."""

    prompt_session: PromptSession = PromptSession(history=FileHistory(_history_file_path()))
    active_session_id = session_id

    def create_session_agent(*, next_session_id: str, messages: list[dict[str, Any]]) -> Agent:
        return Agent(
            model=agent.model,
            provider=agent.provider,
            cwd=agent.cwd,
            session_dir=store.session_dir(next_session_id),
            api_key=agent.api_key,
            api_base=agent.api_base,
            messages=messages,
            max_turns=agent.max_turns,
            max_tokens=agent.max_tokens,
            reasoning_effort=agent.reasoning_effort,
            settings=agent.settings,
        )

    async def on_persist(message: dict) -> None:
        await store.append_message(active_session_id, message)

    while True:
        width = min(shutil.get_terminal_size().columns, _MAX_WIDTH)
        console.print(f"[dim]{'─' * width}[/dim]")

        try:
            user_input: str = await asyncio.get_event_loop().run_in_executor(
                None, lambda: prompt_session.prompt(_PROMPT)
            )
        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print("\n[dim]bye[/dim]")
            return

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input in ("/q", "exit", "quit"):
            console.print("[dim]bye[/dim]")
            return

        if user_input in ("/c", "/clear"):
            await store.clear_session(active_session_id)
            agent.clear()
            console.print("[green]⏺[/green] [dim]cleared[/dim]")
            continue

        # These slash commands only control local session state.
        if user_input == "/new":
            data = await store.create_session(
                None,
                provider=agent.provider,
                model=agent.model,
                cwd=agent.cwd,
                api_base=agent.api_base,
            )
            session = data.get("session") or {}
            active_session_id = str(session.get("id") or "")
            agent = create_session_agent(next_session_id=active_session_id, messages=[])
            print_header(provider=agent.provider, model=agent.model, session=session, mode="new", message_count=0)
            continue

        if user_input == "/resume":
            sessions = await store.list_sessions(cwd=agent.cwd)
            sessions = [item for item in sessions if item.get("id") != active_session_id]
            if not sessions:
                console.print("[dim]no other sessions in this workspace[/dim]")
                continue

            print_session_list(
                sessions,
                current_session_id=active_session_id,
                heading="resume session: enter number, session id prefix, or blank to cancel",
            )

            while True:
                selection = (await _prompt_selection(prompt_session, "\033[1mresume>\033[0m ")).strip()
                if not selection:
                    console.print("[dim]resume cancelled[/dim]")
                    break

                try:
                    selected = _resolve_session_choice(selection, sessions)
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue

                if not selected:
                    console.print("[dim]unknown session selection[/dim]")
                    continue

                active_session_id = str(selected.get("id") or "")
                data = await store.get_or_create(
                    active_session_id,
                    provider=agent.provider,
                    model=agent.model,
                    cwd=agent.cwd,
                    api_base=agent.api_base,
                )
                messages = data.get("messages") or []
                session = data.get("session") or selected
                agent = create_session_agent(next_session_id=active_session_id, messages=messages)
                print_header(
                    provider=agent.provider,
                    model=agent.model,
                    session=session,
                    mode="resumed",
                    message_count=len(messages),
                )
                print_history_preview(messages)
                break

            continue

        if user_input == "/provider":
            provider_options = _provider_options(agent.settings)
            console.print("[dim]switch provider: enter number, provider name, or blank to cancel[/dim]")
            for index, option in enumerate(provider_options, start=1):
                marker = "*" if option.provider == agent.provider and option.api_base == agent.api_base else " "
                models = ", ".join(option.models[:3])
                if len(option.models) > 3:
                    models += f" +{len(option.models) - 3}"
                label = option.name if option.name == option.provider else f"{option.name} ({option.provider})"
                suffix = f" [{models}]" if models else ""
                console.print(f"  {marker}{index:>2}  {label}{suffix}")
            provider_names = [option.name for option in provider_options]

            while True:
                selection = (await _prompt_selection(prompt_session, "\033[1mprovider>\033[0m ")).strip()
                if not selection:
                    console.print("[dim]provider switch cancelled[/dim]")
                    break

                try:
                    selected_name = _select_list_item(selection, provider_names, label="provider")
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue

                if not selected_name:
                    console.print("[dim]unknown provider selection[/dim]")
                    continue

                selected = next(option for option in provider_options if option.name == selected_name)

                try:
                    changed = await _switch_agent_runtime(
                        agent,
                        store=store,
                        session_id=active_session_id,
                        provider_name=selected.name,
                        model=None,
                    )
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                else:
                    if changed:
                        console.print(f"[green]⏺[/green] [dim]provider/model →[/dim] {agent.provider} / {agent.model}")
                    else:
                        console.print(f"[green]⏺[/green] [dim]already using[/dim] {agent.provider} / {agent.model}")
                break

            continue

        if user_input.startswith("/provider "):
            provider_name = user_input[len("/provider ") :].strip()
            if not provider_name:
                console.print("[dim]usage: /provider <name>[/dim]")
                continue
            try:
                changed = await _switch_agent_runtime(
                    agent,
                    store=store,
                    session_id=active_session_id,
                    provider_name=provider_name,
                    model=None,
                )
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
            else:
                if changed:
                    console.print(f"[green]⏺[/green] [dim]provider/model →[/dim] {agent.provider} / {agent.model}")
                else:
                    console.print(f"[green]⏺[/green] [dim]already using[/dim] {agent.provider} / {agent.model}")
            continue

        if user_input == "/model":
            current_provider = _find_provider_option(
                agent.settings,
                provider=agent.provider,
                api_base=agent.api_base,
            )
            # Keep the configured alias when present so model resolution stays on
            # the same provider config (base URL, model list, reasoning settings).
            provider_name = current_provider.name if current_provider else agent.provider
            models = _model_options(
                agent.settings, provider=agent.provider, api_base=agent.api_base, current_model=agent.model
            )
            if not models:
                console.print("[dim]no configured models for the current provider[/dim]")
                continue

            provider_label = provider_name
            if current_provider and current_provider.name != current_provider.provider:
                provider_label = f"{current_provider.name} ({current_provider.provider})"
            console.print(f"[dim]switch model for {provider_label}: enter number, model name, or blank to cancel[/dim]")
            for index, model in enumerate(models, start=1):
                marker = "*" if model == agent.model else " "
                console.print(f"  {marker}{index:>2}  {model}")

            while True:
                selection = (await _prompt_selection(prompt_session, "\033[1mmodel>\033[0m ")).strip()
                if not selection:
                    console.print("[dim]model switch cancelled[/dim]")
                    break

                try:
                    selected_model = _select_list_item(selection, models, label="model")
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                    continue

                if not selected_model:
                    console.print("[dim]unknown model selection[/dim]")
                    continue

                try:
                    changed = await _switch_agent_runtime(
                        agent,
                        store=store,
                        session_id=active_session_id,
                        provider_name=provider_name,
                        model=selected_model,
                    )
                except ValueError as exc:
                    console.print(f"[red]{exc}[/red]")
                else:
                    if changed:
                        console.print(f"[green]⏺[/green] [dim]model →[/dim] {agent.model}")
                    else:
                        console.print(f"[green]⏺[/green] [dim]already using[/dim] {agent.model}")
                break

            continue

        if user_input.startswith("/model "):
            new_model = user_input[len("/model ") :].strip()
            if not new_model:
                console.print("[dim]usage: /model <name>[/dim]")
                continue
            try:
                current_provider = _find_provider_option(
                    agent.settings,
                    provider=agent.provider,
                    api_base=agent.api_base,
                )
                # Same reason as `/model`: prefer the configured alias when the
                # current runtime came from one.
                provider_name = current_provider.name if current_provider else agent.provider
                changed = await _switch_agent_runtime(
                    agent,
                    store=store,
                    session_id=active_session_id,
                    provider_name=provider_name,
                    model=new_model,
                )
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
            else:
                if changed:
                    console.print(f"[green]⏺[/green] [dim]model →[/dim] {agent.model}")
                else:
                    console.print(f"[green]⏺[/green] [dim]already using[/dim] {agent.model}")
            continue

        if user_input.startswith("/"):
            console.print("[dim]unknown: /c  /q  /new  /resume  /provider [name]  /model [name][/dim]")
            continue

        width = min(shutil.get_terminal_size().columns, _MAX_WIDTH)
        console.print(f"[dim]{'─' * width}[/dim]")

        renderer = TUIRenderer(console)
        renderer.start()

        try:
            async for event in agent.achat(user_input, on_persist=on_persist):
                match event.type:
                    case "reasoning":
                        renderer.reasoning(event.data.get("content", ""))
                    case "text":
                        renderer.text(event.data.get("content", ""))
                    case "tool_start":
                        renderer.tool_start(event.data.get("name", ""), event.data.get("args") or {})
                    case "tool_output":
                        renderer.tool_output(event.data.get("content", ""))
                    case "tool_done":
                        renderer.tool_done(event.data.get("result", ""))
                    case "error":
                        renderer.error(event.data.get("message", ""))
        except KeyboardInterrupt:
            agent.cancel()
            renderer.cancel()
            continue

        renderer.finish()


def _run_web_command(*, cwd: str, hostname: str, port: int | None) -> None:
    settings = get_settings(cwd)
    resolved_port = port or settings.port

    if not frontend_dist_path().exists():
        console.print(
            "[yellow]frontend build not found; starting API only. "
            "Run `pnpm --dir mycode/frontend build` to serve the web UI.[/yellow]"
        )

    import uvicorn

    uvicorn.run(create_app(), host=hostname, port=resolved_port)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    cwd = os.path.abspath(os.getcwd())

    if args.command == "web":
        _run_web_command(cwd=cwd, hostname=args.hostname, port=args.port)
        return

    store = SessionStore()

    if args.command == "session" and args.session_command == "list":
        sessions = asyncio.run(store.list_sessions(cwd=None if args.all else cwd))
        heading = "all sessions" if args.all else f"sessions for {cwd}"
        print_session_list(sessions, include_cwd=args.all, heading=heading)
        return

    try:
        settings = get_settings(cwd)
        resolved = resolve_provider(settings, provider_name=args.provider, model=args.model)
        resolved_session = asyncio.run(
            resolve_cli_session(
                store=store,
                provider=resolved.provider,
                cwd=cwd,
                model=resolved.model,
                api_base=resolved.api_base,
                requested_session_id=args.session,
                continue_last=args.continue_last,
            )
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    agent = Agent(
        model=resolved.model,
        provider=resolved.provider,
        cwd=cwd,
        session_dir=store.session_dir(resolved_session.session_id),
        api_key=resolved.api_key,
        api_base=resolved.api_base,
        messages=resolved_session.messages,
        settings=settings,
        reasoning_effort=resolved.reasoning_effort,
        max_tokens=resolved.max_tokens,
        max_turns=args.max_turns,
    )

    if args.command == "run":
        message = " ".join(args.message).strip()
        code = asyncio.run(run_once(agent, store=store, session_id=resolved_session.session_id, message=message))
        raise SystemExit(code)

    print_header(
        provider=resolved.provider,
        model=resolved.model,
        session=resolved_session.session,
        mode=resolved_session.mode,
        message_count=len(resolved_session.messages),
    )
    if resolved_session.mode == "resumed":
        print_history_preview(resolved_session.messages)

    asyncio.run(chat_loop(agent, store=store, session_id=resolved_session.session_id))


if __name__ == "__main__":
    main()
