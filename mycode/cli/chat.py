"""Interactive terminal chat for the CLI."""

from __future__ import annotations

import asyncio
import html
import re
import shlex
from base64 import b64encode
from collections.abc import Iterable
from pathlib import Path
from typing import Any, override

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import RadioList
from rich.text import Text

from mycode.core.agent import Agent
from mycode.core.config import resolve_mycode_home
from mycode.core.messages import build_message, document_block, image_block, text_block
from mycode.core.session import SessionStore
from mycode.core.tools import detect_document_mime_type, detect_image_mime_type, resolve_path

from .render import ReplyRenderer, TerminalView, format_local_timestamp
from .runtime import (
    REASONING_EFFORT_OPTIONS,
    append_session_message,
    clone_agent,
    get_provider_option,
    list_model_options,
    list_provider_options,
    supports_reasoning_effort,
    update_agent_runtime,
    update_reasoning_effort,
)
from .theme import MUTED, PROMPT_CHAR, TERMINAL_THEME, TOOL_MARKER

_PROMPT = ANSI(f"\033[1m\033[34m{PROMPT_CHAR}\033[0m ")

_COMMAND_HELP = (
    ("/clear", "Clear conversation"),
    ("/new", "New session"),
    ("/resume", "Switch session"),
    ("/rewind", "Rewind to a previous message"),
    ("/provider", "Switch provider"),
    ("/model", "Switch model"),
    ("/effort", "Set reasoning effort"),
    ("/q", "Quit"),
)
_SLASH_COMMANDS = tuple(command for command, _ in _COMMAND_HELP)
# Only treat `@path` as a reference when it starts a standalone token.
_AT_PATH_RE = re.compile(r"(?<!\S)@(?:(?P<quote>['\"])(?P<quoted>[^'\"]*)|(?P<plain>[^\s'\"]*))$")


# Style for the focused row in the inline selector.
_FOCUSED_STYLE = "bold blue" if TERMINAL_THEME == "light" else "bold cyan"


class _InlineRadioList[T](RadioList[T]):
    """Arrow-key list that shows > on the focused item and exits on Enter."""

    @override
    def _handle_enter(self) -> None:
        # Only called by Enter/Space (not arrows), so safe to exit.
        self.current_value = self.values[self._selected_index][0]
        get_app().exit(result=self.current_value)

    @override
    def _get_text_fragments(self) -> StyleAndTextTuples:
        # Override rendering: show > based on focus, not checked state.
        result: StyleAndTextTuples = []
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
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    app: Application[T | None] = Application(
        layout=Layout(radio),
        key_bindings=kb,
        full_screen=False,
    )
    return await app.run_async()


class _PromptCompleter(Completer):
    """Complete slash commands and explicit `@path` references for the prompt."""

    _COMMANDS = dict(_COMMAND_HELP)

    def __init__(self, *, cwd: str | None = None) -> None:
        self._cwd = cwd

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        del complete_event
        text_before_cursor = document.text_before_cursor
        text = text_before_cursor.lstrip()
        if self._cwd:
            match = _AT_PATH_RE.search(text_before_cursor)
            if match:
                quote = str(match.group("quote") or "")
                query = str(match.group("quoted") or match.group("plain") or "")
                # Complete only real paths under the current working directory.
                if query == "~":
                    base_prefix = "~/"
                    partial = ""
                    base_dir = Path("~").expanduser()
                elif query.endswith("/"):
                    base_prefix = query
                    partial = ""
                    base_dir = Path(resolve_path(query or ".", cwd=self._cwd))
                else:
                    head, sep, tail = query.rpartition("/")
                    base_prefix = f"{head}{sep}" if sep else ""
                    partial = tail if sep else query
                    base_dir = Path(resolve_path(base_prefix or ".", cwd=self._cwd))

                if base_dir.is_dir():
                    for entry in sorted(base_dir.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                        if partial and not entry.name.startswith(partial):
                            continue
                        candidate = f"{base_prefix}{entry.name}{'/' if entry.is_dir() else ''}"
                        replacement = "@" + shlex.quote(candidate)
                        if quote:
                            replacement = f"@{quote}{candidate}"
                            if not entry.is_dir():
                                replacement += quote
                        yield Completion(
                            replacement,
                            start_position=-len(match.group(0)),
                            display="@" + candidate,
                            display_meta="dir" if entry.is_dir() else "file",
                        )
                return

        if not text.startswith("/"):
            return
        for cmd, desc in self._COMMANDS.items():
            if cmd.startswith(text) and cmd != text:
                yield Completion(cmd, start_position=-len(text), display_meta=desc)


def _rewrite_pasted_file_paths(text: str) -> str | None:
    """Rewrite pasted file paths into explicit `@path` references."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return None
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    paths = [Path(token).expanduser() for token in tokens]
    if not all(path.is_file() for path in paths):
        return None
    return " ".join(f"@{shlex.quote(str(path))}" for path in paths)


def _build_chat_key_bindings() -> KeyBindings:
    """Build key bindings for the main chat prompt."""
    kb = KeyBindings()

    def _clear(event: KeyPressEvent) -> None:
        event.app.renderer.clear()

    kb.add("c-l")(_clear)

    # In multiline mode the default Enter inserts a newline; override it to submit.
    def _submit(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    kb.add("enter", eager=True)(_submit)

    # Esc+Enter (Meta+Enter) inserts a newline for multiline input.
    def _insert_newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    kb.add("escape", "enter")(_insert_newline)

    @kb.add(Keys.BracketedPaste, eager=True)
    def _handle_bracketed_paste(event: KeyPressEvent) -> None:
        pasted = event.data.replace("\r\n", "\n").replace("\r", "\n")
        event.current_buffer.insert_text(_rewrite_pasted_file_paths(pasted) or pasted)

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
        self.prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(history_file_path()),
            completer=_PromptCompleter(cwd=self.agent.cwd),
            key_bindings=_build_chat_key_bindings(),
            multiline=True,
            prompt_continuation="  ",
        )

    async def run(self) -> None:
        """Run the interactive chat loop until the user exits the terminal UI."""

        prefill = ""
        while True:
            self.view.console.print()

            try:
                user_input = await self.prompt_session.prompt_async(_PROMPT, default=prefill)
            except KeyboardInterrupt:
                prefill = ""
                continue
            except EOFError:
                self.view.console.print("\n[dim]bye[/dim]")
                return
            finally:
                prefill = ""

            user_input = user_input.strip()
            if not user_input:
                continue

            result = await self._handle_command(user_input)
            if result == "exit":
                return
            if isinstance(result, str):
                # Command wants to prefill the next prompt (e.g. /rewind).
                prefill = result
                continue
            if result:
                continue

            self.view.console.print()
            renderer = ReplyRenderer(self.view.console)
            user_message = self._build_user_message(user_input)
            try:
                await renderer.render(self.agent, user_message, on_persist=self._persist_message)
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

    def _build_user_message(self, text: str) -> dict[str, Any]:
        """Build one user message with the raw prompt first, then resolved attachments.

        Text files are appended as extra text blocks. Images and PDFs become
        native blocks only when the current model supports that input type.
        Only explicit `@path` tokens that resolve to real files are attached.
        """

        blocks = [text_block(text)]
        try:
            tokens = shlex.split(text.replace("\r\n", "\n").replace("\r", "\n"), posix=True)
        except ValueError:
            return build_message("user", blocks)

        seen: set[str] = set()
        for token in tokens:
            if not token.startswith("@") or token == "@":
                continue

            path = Path(resolve_path(token[1:], cwd=self.agent.cwd))
            if not path.is_file():
                continue

            path_text = str(path)
            if path_text in seen:
                continue
            seen.add(path_text)

            image_mime_type = detect_image_mime_type(path)
            if image_mime_type:
                if self.agent.supports_image_input:
                    image_data = b64encode(path.read_bytes()).decode("utf-8")
                    blocks.append(image_block(image_data, mime_type=image_mime_type, name=path.name))
                else:
                    blocks.append(
                        text_block(
                            f'<file name="{html.escape(path_text, quote=True)}" media_type="{image_mime_type}" kind="image">Current model does not support image input.</file>',
                            meta={"attachment": True, "path": path_text},
                        )
                    )
                continue

            document_mime_type = detect_document_mime_type(path)
            if document_mime_type:
                if getattr(self.agent, "supports_pdf_input", False):
                    document_data = b64encode(path.read_bytes()).decode("utf-8")
                    blocks.append(document_block(document_data, mime_type=document_mime_type, name=path.name))
                else:
                    blocks.append(
                        text_block(
                            f'<file name="{html.escape(path_text, quote=True)}" media_type="{document_mime_type}" kind="document">Current model does not support PDF input.</file>',
                            meta={"attachment": True, "path": path_text},
                        )
                    )
                continue

            # Reuse the existing read tool so attached text files follow the same
            # UTF-8, truncation, and long-line rules as agent-initiated reads.
            result = self.agent.tools.read(path=path_text)
            if result.is_error:
                continue

            blocks.append(
                text_block(
                    f'<file name="{html.escape(path_text, quote=True)}">\n{result.model_text}\n</file>',
                    meta={"attachment": True, "path": path_text},
                )
            )

        return build_message("user", blocks)

    async def _persist_message(self, message: dict[str, Any]) -> None:
        """Persist one streamed message into the active session."""

        await append_session_message(self.store, self.session_id, message, agent=self.agent)

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
        matches = [candidate for candidate in _SLASH_COMMANDS if candidate.startswith(command)]
        if len(matches) == 1:
            command = matches[0]

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
            case "/rewind":
                prefill = await self._rewind()
                if prefill:
                    return prefill
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
            ("/rewind", "Rewind to a previous message"),
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

    def _print_runtime_status(self, action: str, value: str, *, changed: bool) -> None:
        """Print the result of a runtime-only change."""

        if changed:
            self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]{action} →[/dim] {value}")
            return
        self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]already using[/dim] {value}")

    def _supports_effort_or_warn(self) -> bool:
        """Return whether the current model supports reasoning effort."""

        if supports_reasoning_effort(self.agent):
            return True
        self.view.console.print("[dim]current model does not support reasoning effort[/dim]")
        return False

    async def _start_new_session(self) -> None:
        """Start a fresh session while keeping the current runtime settings."""

        data = self.store.draft_session(
            None,
            provider=self.agent.provider,
            model=self.agent.model,
            cwd=self.agent.cwd,
            api_base=self.agent.api_base,
        )
        session = data.get("session") or {}
        self.session_id = str(session.get("id") or "")
        self.agent = clone_agent(self.agent, store=self.store, session_id=self.session_id, messages=[])
        self.view.print_header(
            provider=self.agent.provider,
            model=self.agent.model,
            session=session,
            mode="new",
            message_count=0,
            reasoning_effort=self.agent.reasoning_effort,
        )

    async def _rewind(self) -> str | None:
        """Rewind the conversation to a chosen user message.

        Shows an interactive selector of all real user text messages.
        Selecting one truncates the in-memory conversation to the slice before
        that user message index and appends a rewind marker to the session log.
        Returns the original message text to prefill the next prompt.
        """
        messages = self.agent.messages
        if not messages:
            self.view.console.print("[dim]nothing to rewind[/dim]")
            return None

        # Collect real user text messages (skip synthetic compact summaries
        # and tool-result-only user messages).
        user_turns: dict[int, str] = {}  # message_index -> text
        for i, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            if (msg.get("meta") or {}).get("synthetic"):
                continue
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    user_turns[i] = str(b["text"]).strip()
                    break

        if not user_turns:
            self.view.console.print("[dim]no user messages to rewind to[/dim]")
            return None

        # Build selector options — most recent first.
        options: list[tuple[int, str]] = []
        for msg_index, text in reversed(list(user_turns.items())):
            preview = text.replace("\n", " ")[:60]
            if len(text) > 60:
                preview += "..."
            options.append((msg_index, preview))

        selected = await choose(options)
        if selected is None:
            return None

        original_text = user_turns.get(selected, "")

        # Persist the rewind event and truncate in-memory messages.
        await self.store.append_rewind(self.session_id, selected)
        self.agent.messages = messages[:selected]

        self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]rewound[/dim]")
        if self.agent.messages:
            self.view.print_history_preview(self.agent.messages)
        else:
            self.view.console.print("[dim]conversation is now empty[/dim]")

        return original_text

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
            ts = format_local_timestamp(str(s.get("updated_at") or ""), "%m-%d %H:%M")
            label = f"{title}  {ts}" if ts else title
            options.append((s, label))

        session = await choose(options)
        if session is None:
            return

        self.session_id = str(session.get("id") or "")
        data = await self.store.load_session(self.session_id)
        if not data:
            self.view.console.print("[red]failed to load session[/red]")
            return
        messages = data.get("messages") or []
        loaded_session = data.get("session") or session
        self.agent = clone_agent(self.agent, store=self.store, session_id=self.session_id, messages=messages)
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
        current = get_provider_option(self.agent.settings, provider=self.agent.provider, api_base=self.agent.api_base)

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
                provider_name=provider_name,
                model=None,
            )
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        label = f"{self.agent.provider} / {self.agent.model}"
        if self.agent.reasoning_effort:
            label += f" [effort: {self.agent.reasoning_effort}]"
        self._print_runtime_status("provider/model", label, changed=changed)

    async def _apply_model_change(self, model_name: str) -> None:
        """Switch the active model for the current provider runtime."""

        current = get_provider_option(self.agent.settings, provider=self.agent.provider, api_base=self.agent.api_base)
        provider_name = current.name if current else self.agent.provider

        try:
            changed = await update_agent_runtime(
                self.agent,
                provider_name=provider_name,
                model=model_name,
            )
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        self._print_runtime_status("model", self.agent.model, changed=changed)

    async def _switch_effort(self) -> None:
        """Prompt for a reasoning effort level."""

        if not self._supports_effort_or_warn():
            return

        current = self.agent.reasoning_effort or "auto"
        choices = [(o, o) for o in REASONING_EFFORT_OPTIONS]
        selected = await choose(choices, default=current)
        if selected is not None:
            self._apply_effort_change(selected)

    def _apply_effort_change(self, effort: str) -> None:
        """Apply a reasoning effort change to the active agent."""

        if not self._supports_effort_or_warn():
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
        self._print_runtime_status("effort", display, changed=changed)
