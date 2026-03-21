"""Interactive terminal chat for the CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import completion_is_selected
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.table import Table
from rich.text import Text

from mycode.core.agent import Agent
from mycode.core.config import resolve_mycode_home
from mycode.core.session import SessionStore

from .render import ReplyRenderer, TerminalView
from .runtime import ProviderOption, list_model_options, list_provider_options, update_agent_runtime
from .theme import MUTED, PROMPT_CHAR, SUCCESS, TOOL_MARKER, TOOL_NAME

_PROMPT = ANSI(f"\033[1m\033[34m{PROMPT_CHAR}\033[0m ")
_EXIT_COMMANDS = {"/q", "exit", "quit"}
_CLEAR_COMMANDS = {"/c", "/clear"}


class _SlashCompleter(Completer):
    """Auto-complete slash commands."""

    _COMMANDS = {
        "/clear": "Clear conversation",
        "/new": "New session",
        "/resume": "Switch session",
        "/provider": "Switch provider",
        "/model": "Switch model",
        "/q": "Quit",
    }

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return
        for cmd, desc in self._COMMANDS.items():
            if cmd.startswith(text) and cmd != text:
                yield Completion(cmd, start_position=-len(text), display_meta=desc)


def _build_key_bindings() -> KeyBindings:
    """Build prompt key bindings for the interactive session."""
    kb = KeyBindings()

    kb.add("c-l")(lambda event: event.app.renderer.clear())

    # In multiline mode the default Enter inserts a newline; override it to submit.
    kb.add("enter", filter=~completion_is_selected, eager=True)(
        lambda event: event.current_buffer.validate_and_handle()
    )

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
            key_bindings=_build_key_bindings(),
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

            if await self._handle_command(user_input):
                if user_input in _EXIT_COMMANDS:
                    return
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

    async def _handle_command(self, text: str) -> bool:
        """Handle one slash-style command and return whether it was consumed."""

        if text in _EXIT_COMMANDS:
            self.view.console.print("[dim]bye[/dim]")
            return True

        if text in _CLEAR_COMMANDS:
            await self.store.clear_session(self.session_id)
            self.agent.clear()
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]cleared[/dim]")
            return True

        if text == "/new":
            await self._start_new_session()
            return True

        if text == "/resume":
            await self._resume_session()
            return True

        if not text.startswith("/"):
            return False

        command, _, argument = text.partition(" ")
        argument = argument.strip()

        if command == "/provider":
            if argument:
                await self._apply_provider_change(argument)
            else:
                await self._switch_provider()
            return True

        if command == "/model":
            if argument:
                await self._apply_model_change(argument)
            else:
                await self._switch_model()
            return True

        self._print_help()
        return True

    def _print_help(self) -> None:
        commands = [
            ("/c, /clear", "Clear conversation"),
            ("/new", "New session"),
            ("/resume", "Switch session"),
            ("/provider [name]", "Switch provider"),
            ("/model [name]", "Switch model"),
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
        )

    async def _resume_session(self) -> None:
        """Switch to another saved session in the current workspace."""

        sessions = await self.store.list_sessions(cwd=self.agent.cwd)
        sessions = [session for session in sessions if session.get("id") != self.session_id]
        if not sessions:
            self.view.console.print("[dim]no other sessions in this workspace[/dim]")
            return

        self.view.print_session_list(
            sessions,
            current_session_id=self.session_id,
            heading="resume session: enter number, session id prefix, or blank to cancel",
        )

        while True:
            selection = await self._prompt("\033[1mresume>\033[0m ")
            if not selection:
                self.view.console.print("[dim]resume cancelled[/dim]")
                return

            try:
                session = self._select_by_number_or_prefix(
                    selection,
                    sessions,
                    label="session id",
                    text_of=lambda item: str(item.get("id") or ""),
                )
            except ValueError as exc:
                self.view.console.print(f"[red]{exc}[/red]")
                continue

            if session is None:
                self.view.console.print("[dim]unknown session selection[/dim]")
                continue

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
            )
            self.view.print_history_preview(messages)
            return

    async def _switch_provider(self) -> None:
        """Prompt for a configured provider and apply it to the active agent."""

        options = list_provider_options(self.agent.settings)

        self.view.console.print()
        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), expand=False)
        table.add_column(no_wrap=True)  # marker
        table.add_column(no_wrap=True)  # index
        table.add_column(no_wrap=True)  # name
        table.add_column()  # models

        for index, option in enumerate(options, start=1):
            is_current = option.provider == self.agent.provider and option.api_base == self.agent.api_base
            marker = Text("●", style=SUCCESS) if is_current else Text(" ")
            idx = Text(str(index), style=MUTED)
            name = Text(option.name, style=TOOL_NAME if is_current else "")

            models_str = ""
            if option.models:
                models_str = "  ".join(option.models[:3])
                if len(option.models) > 3:
                    models_str += f"  +{len(option.models) - 3}"
            models_text = Text(models_str, style=MUTED)

            table.add_row(marker, idx, name, models_text)

        self.view.console.print(table)

        while True:
            selection = await self._prompt("\033[1mprovider>\033[0m ")
            if not selection:
                return

            try:
                option = self._select_by_number_or_prefix(
                    selection, options, label="provider", text_of=lambda item: item.name
                )
            except ValueError as exc:
                self.view.console.print(f"[red]{exc}[/red]")
                continue

            if option is None:
                self.view.console.print(f"[red]unknown provider: {selection}[/red]")
                continue

            await self._apply_provider_change(option.name)
            return

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

        self.view.console.print()
        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), expand=False)
        table.add_column(no_wrap=True)  # marker
        table.add_column(no_wrap=True)  # index
        table.add_column()  # model name

        for index, model in enumerate(models, start=1):
            is_current = model == self.agent.model
            marker = Text("●", style=SUCCESS) if is_current else Text(" ")
            idx = Text(str(index), style=MUTED)
            name = Text(model, style=TOOL_NAME if is_current else "")
            table.add_row(marker, idx, name)

        self.view.console.print(table)

        while True:
            selection = await self._prompt("\033[1mmodel>\033[0m ")
            if not selection:
                return

            try:
                model = self._select_by_number_or_prefix(selection, models, label="model", text_of=lambda item: item)
            except ValueError as exc:
                self.view.console.print(f"[red]{exc}[/red]")
                continue

            if model is None:
                self.view.console.print(f"[red]unknown model: {selection}[/red]")
                continue

            await self._apply_model_change(model)
            return

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

        if changed:
            self.view.console.print(
                f"[green]{TOOL_MARKER}[/green] [dim]provider/model →[/dim] {self.agent.provider} / {self.agent.model}"
            )
        else:
            self.view.console.print(
                f"[green]{TOOL_MARKER}[/green] [dim]already using[/dim] {self.agent.provider} / {self.agent.model}"
            )

    async def _apply_model_change(self, model_name: str) -> None:
        """Switch the active model for the current provider runtime."""

        option = self._current_provider_option()
        provider_name = option.name if option else self.agent.provider

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

    def _current_provider_option(self) -> ProviderOption | None:
        """Return the configured provider option matching the active runtime."""

        for option in list_provider_options(self.agent.settings):
            if option.provider == self.agent.provider and option.api_base == self.agent.api_base:
                return option
        return None

    async def _prompt(self, prompt_text: str) -> str:
        try:
            value = await self.prompt_session.prompt_async(ANSI(prompt_text), multiline=False)
        except (KeyboardInterrupt, EOFError):
            return ""
        return value.strip()

    @staticmethod
    def _select_by_number_or_prefix[T](
        selection: str,
        items: list[T],
        *,
        label: str,
        text_of: Callable[[T], str],
    ) -> T | None:
        value = selection.strip()
        if not value:
            return None

        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(items):
                return items[index]
            return None

        lowered = value.lower()
        exact = [item for item in items if text_of(item).lower() == lowered]
        if len(exact) == 1:
            return exact[0]

        matches = [item for item in items if text_of(item).lower().startswith(lowered)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous {label}: {value}")
        return None
