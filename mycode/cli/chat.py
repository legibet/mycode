"""Interactive terminal chat for the CLI."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import RadioList
from rich.text import Text

from mycode.core.agent import Agent
from mycode.core.config import resolve_mycode_home
from mycode.core.session import SessionStore

from .render import ReplyRenderer, TerminalView
from .runtime import (
    REASONING_EFFORT_OPTIONS,
    list_model_options,
    list_provider_options,
    supports_reasoning_effort,
    update_agent_runtime,
    update_reasoning_effort,
)
from .theme import MUTED, PROMPT_CHAR, TERMINAL_THEME, TOOL_MARKER

_PROMPT = ANSI(f"\033[1m\033[34m{PROMPT_CHAR}\033[0m ")

# All primary slash commands for prefix resolution.
_SLASH_COMMANDS = ("/clear", "/new", "/resume", "/provider", "/model", "/effort", "/q")


def _resolve_slash(command: str) -> str:
    """Resolve a partial /command to its full form via unique prefix match."""

    matches = [c for c in _SLASH_COMMANDS if c.startswith(command)]
    return matches[0] if len(matches) == 1 else command


# Style for the focused row in the inline selector.
_FOCUSED_STYLE = "bold blue" if TERMINAL_THEME == "light" else "bold cyan"


class _InlineRadioList[T](RadioList):
    """Arrow-key list that shows > on the focused item and exits on Enter."""

    def _handle_enter(self) -> None:
        # Only called by Enter/Space (not arrows), so safe to exit.
        self.current_value = self.values[self._selected_index][0]
        get_app().exit(result=self.current_value)

    def _get_text_fragments(self):
        # Override rendering: show > based on focus, not checked state.
        result: list[tuple[str, str]] = []
        for i, (_value, text) in enumerate(self.values):
            focused = i == self._selected_index
            style = _FOCUSED_STYLE if focused else ""
            result.append((style, "> " if focused else "  "))
            result.append((style, str(text)))
            result.append(("", "\n"))
        result.pop()  # remove trailing newline
        return result


async def choose[T](options: list[tuple[T, str]], *, default: T | None = None) -> T | None:
    """Inline arrow-key selector. Returns the selected value or None on cancel."""

    radio = _InlineRadioList(
        values=options,
        default=default,
        show_scrollbar=False,
        show_cursor=False,
    )

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    app: Application[T | None] = Application(
        layout=Layout(radio),
        key_bindings=kb,
        full_screen=False,
    )
    return await app.run_async()


class _SlashCompleter(Completer):
    """Auto-complete slash commands."""

    _COMMANDS = {
        "/clear": "Clear conversation",
        "/new": "New session",
        "/resume": "Switch session",
        "/provider": "Switch provider",
        "/model": "Switch model",
        "/effort": "Set reasoning effort",
        "/q": "Quit",
    }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        for cmd, desc in self._COMMANDS.items():
            if cmd.startswith(text) and cmd != text:
                yield Completion(cmd, start_position=-len(text), display_meta=desc)


def _build_chat_key_bindings() -> KeyBindings:
    """Build key bindings for the main chat prompt."""
    kb = KeyBindings()

    kb.add("c-l")(lambda event: event.app.renderer.clear())

    # In multiline mode the default Enter inserts a newline; override it to submit.
    kb.add("enter", eager=True)(lambda event: event.current_buffer.validate_and_handle())

    # Esc+Enter (Meta+Enter) inserts a newline for multiline input.
    kb.add("escape", "enter")(lambda event: event.current_buffer.insert_text("\n"))

    return kb


def history_file_path() -> str:
    """Return the path used by prompt-toolkit to store CLI history."""

    path = resolve_mycode_home() / "cli_history"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


class TerminalChat:
    """Own the interactive TUI session, including slash commands and rendering."""

    def __init__(
        self,
        *,
        agent: Agent,
        store: SessionStore,
        session_id: str,
        view: TerminalView | None = None,
    ) -> None:
        self.agent = agent
        self.store = store
        self.session_id = session_id
        self.view = view or TerminalView()
        self.prompt_session = PromptSession(
            history=FileHistory(history_file_path()),
            completer=_SlashCompleter(),
            key_bindings=_build_chat_key_bindings(),
            multiline=True,
            prompt_continuation="  ",
        )

    async def run(self) -> None:
        """Run the interactive chat loop until the user exits the terminal UI."""

        while True:
            self.view.console.print()

            try:
                user_input = await self.prompt_session.prompt_async(_PROMPT)
            except KeyboardInterrupt:
                continue
            except EOFError:
                self.view.console.print("\n[dim]bye[/dim]")
                return

            user_input = user_input.strip()
            if not user_input:
                continue

            result = await self._handle_command(user_input)
            if result == "exit":
                return
            if result:
                continue

            self.view.console.print()
            renderer = ReplyRenderer(self.view.console)
            try:
                await renderer.render(self.agent, user_input, on_persist=self._persist_message)
            except (KeyboardInterrupt, asyncio.CancelledError):
                self.agent.cancel()
                renderer.cancel()
                # Python 3.11+: uncancel the task so the loop can continue after Ctrl+C.
                task = asyncio.current_task()
                if task is not None:
                    try:
                        task.uncancel()
                    except AttributeError:
                        pass  # Python < 3.11

    async def _persist_message(self, message: dict[str, Any]) -> None:
        """Persist one streamed message into the active session."""

        await self.store.append_message(self.session_id, message)

    def _clone_agent_for_session(self, *, session_id: str, messages: list[dict[str, Any]]) -> Agent:
        """Clone the current agent configuration for a different session state."""

        return Agent(
            model=self.agent.model,
            provider=self.agent.provider,
            cwd=self.agent.cwd,
            session_dir=self.store.session_dir(session_id),
            session_id=session_id,
            api_key=self.agent.api_key,
            api_base=self.agent.api_base,
            messages=messages,
            max_turns=self.agent.max_turns,
            max_tokens=self.agent.max_tokens,
            reasoning_effort=self.agent.reasoning_effort,
            settings=self.agent.settings,
        )

    async def _handle_command(self, text: str) -> str | bool:
        """Handle a slash command. Returns "exit" to quit, True if consumed, False otherwise."""

        # Non-slash exit aliases.
        if text in ("exit", "quit"):
            self.view.console.print("[dim]bye[/dim]")
            return "exit"

        if not text.startswith("/"):
            return False

        command, _, argument = text.partition(" ")
        argument = argument.strip()
        command = _resolve_slash(command)

        match command:
            case "/q":
                self.view.console.print("[dim]bye[/dim]")
                return "exit"
            case "/c" | "/clear":
                await self.store.clear_session(self.session_id)
                self.agent.clear()
                self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]cleared[/dim]")
            case "/new":
                await self._start_new_session()
            case "/resume":
                await self._resume_session()
            case "/provider":
                if argument:
                    await self._apply_provider_change(argument)
                else:
                    await self._switch_provider()
            case "/model":
                if argument:
                    await self._apply_model_change(argument)
                else:
                    await self._switch_model()
            case "/effort":
                if argument:
                    self._apply_effort_change(argument)
                else:
                    await self._switch_effort()
            case _:
                self._print_help()

        return True

    def _print_help(self) -> None:
        commands = [
            ("/c, /clear", "Clear conversation"),
            ("/new", "New session"),
            ("/resume", "Switch session"),
            ("/provider [name]", "Switch provider"),
            ("/model [name]", "Switch model"),
            ("/effort [level]", "Set reasoning effort"),
            ("/q", "Quit"),
        ]
        self.view.console.print()
        for cmd, desc in commands:
            line = Text()
            line.append(f"  {cmd:<20}", style="bold")
            line.append(desc, style=MUTED)
            self.view.console.print(line)

    async def _start_new_session(self) -> None:
        """Start a fresh session while keeping the current runtime settings."""

        data = await self.store.create_session(
            None,
            provider=self.agent.provider,
            model=self.agent.model,
            cwd=self.agent.cwd,
            api_base=self.agent.api_base,
        )
        session = data.get("session") or {}
        self.session_id = str(session.get("id") or "")
        self.agent = self._clone_agent_for_session(session_id=self.session_id, messages=[])
        self.view.print_header(
            provider=self.agent.provider,
            model=self.agent.model,
            session=session,
            mode="new",
            message_count=0,
            reasoning_effort=self.agent.reasoning_effort,
        )

    async def _resume_session(self) -> None:
        """Switch to another saved session in the current workspace."""

        sessions = await self.store.list_sessions(cwd=self.agent.cwd)
        sessions = [s for s in sessions if s.get("id") != self.session_id]
        if not sessions:
            self.view.console.print("[dim]no other sessions in this workspace[/dim]")
            return

        options: list[tuple[dict[str, Any], str]] = []
        for s in sessions:
            title = str(s.get("title") or "New chat")[:40]
            ts = self._format_session_time(str(s.get("updated_at") or ""))
            label = f"{title}  {ts}" if ts else title
            options.append((s, label))

        session = await choose(options)
        if session is None:
            return

        self.session_id = str(session.get("id") or "")
        data = await self.store.get_or_create(
            self.session_id,
            provider=self.agent.provider,
            model=self.agent.model,
            cwd=self.agent.cwd,
            api_base=self.agent.api_base,
        )
        messages = data.get("messages") or []
        loaded_session = data.get("session") or session
        self.agent = self._clone_agent_for_session(session_id=self.session_id, messages=messages)
        self.view.print_header(
            provider=self.agent.provider,
            model=self.agent.model,
            session=loaded_session,
            mode="resumed",
            message_count=len(messages),
            reasoning_effort=self.agent.reasoning_effort,
        )
        self.view.print_history_preview(messages)

    async def _switch_provider(self) -> None:
        """Prompt for a configured provider and apply it to the active agent."""

        options = list_provider_options(self.agent.settings)
        current = next(
            (o for o in options if o.provider == self.agent.provider and o.api_base == self.agent.api_base),
            None,
        )

        choices: list[tuple[str, str]] = []
        for option in options:
            models = "  ".join(option.models[:3])
            if len(option.models) > 3:
                models += f"  +{len(option.models) - 3}"
            choices.append((option.name, f"{option.name}  {models}"))

        selected = await choose(choices, default=current.name if current else None)
        if selected is not None:
            await self._apply_provider_change(selected)

    async def _switch_model(self) -> None:
        """Prompt for a model supported by the current provider runtime."""

        models = list_model_options(
            self.agent.settings,
            provider=self.agent.provider,
            api_base=self.agent.api_base,
            current_model=self.agent.model,
        )
        if not models:
            self.view.console.print("[dim]no configured models for the current provider[/dim]")
            return

        choices = [(m, m) for m in models]
        selected = await choose(choices, default=self.agent.model)
        if selected is not None:
            await self._apply_model_change(selected)

    async def _apply_provider_change(self, provider_name: str) -> None:
        """Switch the active provider, keeping session history unchanged."""

        try:
            changed = await update_agent_runtime(
                self.agent,
                store=self.store,
                session_id=self.session_id,
                provider_name=provider_name,
                model=None,
            )
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        label = f"{self.agent.provider} / {self.agent.model}"
        if self.agent.reasoning_effort:
            label += f" [effort: {self.agent.reasoning_effort}]"
        if changed:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]provider/model →[/dim] {label}")
        else:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]already using[/dim] {label}")

    async def _apply_model_change(self, model_name: str) -> None:
        """Switch the active model for the current provider runtime."""

        options = list_provider_options(self.agent.settings)
        current = next(
            (o for o in options if o.provider == self.agent.provider and o.api_base == self.agent.api_base),
            None,
        )
        provider_name = current.name if current else self.agent.provider

        try:
            changed = await update_agent_runtime(
                self.agent,
                store=self.store,
                session_id=self.session_id,
                provider_name=provider_name,
                model=model_name,
            )
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        if changed:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]model →[/dim] {self.agent.model}")
        else:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]already using[/dim] {self.agent.model}")

    async def _switch_effort(self) -> None:
        """Prompt for a reasoning effort level."""

        if not supports_reasoning_effort(self.agent):
            self.view.console.print("[dim]current model does not support reasoning effort[/dim]")
            return

        current = self.agent.reasoning_effort or "auto"
        choices = [(o, o) for o in REASONING_EFFORT_OPTIONS]
        selected = await choose(choices, default=current)
        if selected is not None:
            self._apply_effort_change(selected)

    def _apply_effort_change(self, effort: str) -> None:
        """Apply a reasoning effort change to the active agent."""

        if not supports_reasoning_effort(self.agent):
            self.view.console.print("[dim]current model does not support reasoning effort[/dim]")
            return

        cleaned = effort.strip().lower()
        if cleaned in ("auto", ""):
            resolved = None
        elif cleaned in REASONING_EFFORT_OPTIONS:
            resolved = cleaned
        else:
            self.view.console.print(f"[red]unknown effort: {effort}[/red]")
            return

        changed = update_reasoning_effort(self.agent, resolved)
        display = resolved or "default"
        if changed:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]effort →[/dim] {display}")
        else:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]already using[/dim] {display}")

    @staticmethod
    def _format_session_time(value: str) -> str:
        if not value:
            return ""
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ts.astimezone().strftime("%m-%d %H:%M")
        except ValueError:
            return value[:16].replace("T", " ")
