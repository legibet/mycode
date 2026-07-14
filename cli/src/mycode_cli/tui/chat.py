"""Interactive terminal chat for the CLI."""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
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

from mycode.agent import Agent
from mycode.attachments import (
    Attachment,
    build_attachment_blocks,
    detect_document_mime_type,
    detect_image_mime_type,
    unsupported_attachment_block,
)
from mycode.compact import NothingToCompactError
from mycode.messages import ConversationMessage, build_message, flatten_message_text, text_block
from mycode.providers import (
    get_provider_adapter,
    list_env_discoverable_providers,
    provider_api_key_from_env,
    provider_default_models,
)
from mycode.session import SessionStore
from mycode.tools import bash_tool, edit_tool, read_tool, write_tool
from mycode.utils import resolve_path
from mycode_cli.config import (
    REASONING_EFFORT_OPTIONS,
    ResolvedProvider,
    Settings,
    get_settings,
    normalize_reasoning_effort,
    provider_has_api_key,
    resolve_mycode_home,
    resolve_provider,
)
from mycode_cli.permissions import ToolReviewDecision, ToolReviewRequest, build_permission_hooks
from mycode_cli.system_prompt import build_skill_snapshot_blocks, discover_slash_skills

from .render import ReplyRenderer, TerminalView, format_local_timestamp
from .theme import ERROR, ERROR_MARKER, MUTED, PROMPT_CHAR, TERMINAL_THEME, TOOL_MARKER, WARNING

_PROMPT = ANSI(f"\033[1m\033[34m{PROMPT_CHAR}\033[0m ")

# (command, help usage, description) — the completer offers `command`, help prints `usage`.
_COMMANDS = (
    ("/clear", "/c, /clear", "Clear conversation"),
    ("/compact", "/compact", "Compact conversation context"),
    ("/new", "/new", "New session"),
    ("/resume", "/resume", "Switch session"),
    ("/rewind", "/rewind", "Rewind to a previous message"),
    ("/provider", "/provider [name]", "Switch provider"),
    ("/model", "/model [name]", "Switch model"),
    ("/effort", "/effort [level]", "Set reasoning effort"),
    ("/q", "/q", "Quit"),
)
_SLASH_COMMANDS = tuple(command for command, _, _ in _COMMANDS)
# Only treat `@path` as a reference when it starts a standalone token.
_AT_PATH_RE = re.compile(r"""(?<!\S)@(?:'(?P<single>[^']*)'?$|"(?P<double>[^"]*)"?$|(?P<plain>[^\s'"]*))$""")
_SKILL_TOKEN_RE = re.compile(r"(?<!\S)/(?P<name>[a-zA-Z0-9_-]*)$")


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
    """Complete built-in commands, skill references, and `@path` references."""

    def __init__(self, *, cwd: str | None = None) -> None:
        self._cwd = cwd
        self._skills = discover_slash_skills(cwd) if cwd else []

    @override
    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        del complete_event
        text_before_cursor = document.text_before_cursor
        if self._cwd:
            match = _AT_PATH_RE.search(text_before_cursor)
            if match:
                yield from self._complete_path(match, self._cwd)
                return

        text = text_before_cursor.lstrip()
        if re.fullmatch(r"/\S*", text):
            for cmd, _usage, desc in _COMMANDS:
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)

        skill_match = _SKILL_TOKEN_RE.search(text_before_cursor)
        if not skill_match:
            return
        query = skill_match.group("name")
        for skill in self._skills:
            if skill.name.startswith(query):
                yield Completion(
                    f"/{skill.name}",
                    start_position=-len(skill_match.group(0)),
                    display=f"/{skill.name}",
                    display_meta=skill.description,
                )

    def _complete_path(self, match: re.Match[str], cwd: str) -> Iterable[Completion]:
        """Yield `@path` completions for real entries under the working directory."""

        if (query := match.group("single")) is not None:
            quote = "'"
        elif (query := match.group("double")) is not None:
            quote = '"'
        else:
            quote = ""
            query = str(match.group("plain") or "")

        if query == "~":
            base_prefix = "~/"
            partial = ""
            base_dir = Path("~").expanduser()
        elif query.endswith("/"):
            base_prefix = query
            partial = ""
            base_dir = Path(resolve_path(query or ".", cwd=cwd))
        else:
            head, sep, tail = query.rpartition("/")
            base_prefix = f"{head}{sep}" if sep else ""
            partial = tail if sep else query
            base_dir = Path(resolve_path(base_prefix or ".", cwd=cwd))

        if not base_dir.is_dir():
            return
        for entry in sorted(base_dir.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if partial and not entry.name.startswith(partial):
                continue
            candidate = f"{base_prefix}{entry.name}{'/' if entry.is_dir() else ''}"
            if quote:
                replacement = f"@{quote}{candidate}{quote}"
            elif any(ch.isspace() for ch in candidate):
                replacement = "@" + shlex.quote(candidate)
            else:
                replacement = "@" + candidate
            yield Completion(
                replacement,
                start_position=-len(match.group(0)),
                display="@" + candidate,
                display_meta="dir" if entry.is_dir() else "file",
            )


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


async def _restart_completion(buffer: Buffer) -> None:
    """Restart completion on the next loop tick after accepting a directory."""

    await asyncio.sleep(0)
    buffer.start_completion(select_first=True)


def resolve_slash_command(command: str) -> str | None:
    """Return the canonical slash command for an exact or unique-prefix match."""

    if command in _SLASH_COMMANDS:
        return command
    matches = [candidate for candidate in _SLASH_COMMANDS if candidate.startswith(command)]
    return matches[0] if len(matches) == 1 else None


def _matched_slash_command(text_before_cursor: str) -> tuple[str, str] | None:
    text = text_before_cursor.lstrip()
    if not text.startswith("/"):
        return None

    command, _, _argument = text.partition(" ")
    resolved = resolve_slash_command(command)
    return (command, resolved) if resolved else None


def _replace_slash_command(buffer: Buffer, command: str, replacement: str) -> None:
    if command == replacement:
        return

    before_cursor = buffer.document.text_before_cursor
    stripped = before_cursor.lstrip()
    command_start = len(before_cursor) - len(stripped)
    command_end = command_start + len(command)
    original_cursor = buffer.cursor_position

    buffer.cursor_position = command_end
    buffer.delete_before_cursor(len(command))
    buffer.insert_text(replacement)
    buffer.cursor_position = original_cursor + len(replacement) - len(command)


def _build_chat_key_bindings() -> KeyBindings:
    """Build key bindings for the main chat prompt."""
    kb = KeyBindings()

    def _clear(event: KeyPressEvent) -> None:
        event.app.renderer.clear()

    kb.add("c-l")(_clear)

    # In multiline mode the default Enter inserts a newline; override it to submit.
    @kb.add("enter", eager=True)
    def _submit_or_complete(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is None or not state.completions:
            buffer.validate_and_handle()
            return

        if slash_command := _matched_slash_command(buffer.document.text_before_cursor):
            _replace_slash_command(buffer, *slash_command)
            buffer.validate_and_handle()
            return

        completion = state.current_completion or state.completions[0]
        buffer.apply_completion(completion)
        if completion.text.startswith("/"):
            buffer.insert_text(" ")
        elif completion.display_meta_text == "dir":
            get_app().create_background_task(_restart_completion(buffer))

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


@dataclass(frozen=True)
class ProviderOption:
    """A provider option shown in the interactive provider switcher."""

    name: str
    provider: str
    models: tuple[str, ...]
    api_base: str | None


def clone_agent(agent: Agent, *, store: SessionStore, session_id: str) -> Agent:
    """Keep the current runtime config while swapping session state.

    History auto-loads from disk when ``session_id`` exists under the store.
    """

    return Agent(
        model=agent.model,
        provider=agent.provider,
        cwd=agent.cwd,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=agent.api_key,
        api_base=agent.api_base,
        max_turns=agent.max_turns,
        max_tokens=agent.max_tokens,
        context_window=agent.context_window,
        compact_threshold=agent.compact_threshold,
        reasoning_effort=agent.reasoning_effort,
        supports_reasoning=agent.supports_reasoning,
        supports_image_input=agent.supports_image_input,
        supports_pdf_input=agent.supports_pdf_input,
        system=agent.system,
        tools=[read_tool, write_tool, edit_tool, bash_tool],
        hooks=agent.hooks,
    )


def list_provider_options(settings: Settings) -> list[ProviderOption]:
    """Return configured providers plus env-discovered built-ins."""

    options: list[ProviderOption] = []
    configured_types: set[str] = set()

    for name, config in settings.providers.items():
        options.append(
            ProviderOption(
                name=name,
                provider=config.type,
                models=tuple(config.models),
                api_base=config.base_url,
            )
        )
        if provider_has_api_key(config):
            configured_types.add(config.type)

    for provider_name in list_env_discoverable_providers():
        if provider_name in configured_types or not provider_api_key_from_env(provider_name):
            continue
        options.append(
            ProviderOption(
                name=provider_name,
                provider=provider_name,
                models=provider_default_models(provider_name),
                api_base=None,
            )
        )

    return options


def get_provider_option(settings: Settings, *, provider: str, api_base: str | None) -> ProviderOption | None:
    """Return the current selectable provider option."""

    for option in list_provider_options(settings):
        if option.provider == provider and option.api_base == api_base:
            return option
    return None


def list_model_options(settings: Settings, *, provider: str, api_base: str | None, current_model: str) -> list[str]:
    """Return the selectable model list for the current provider runtime."""

    option = get_provider_option(settings, provider=provider, api_base=api_base)
    models = option.models if option else provider_default_models(provider)
    return list(dict.fromkeys([current_model, *models]))


def supports_reasoning_effort(agent: Agent) -> bool:
    """Return whether the current agent provider+model supports reasoning effort."""

    return agent.supports_reasoning is True and get_provider_adapter(agent.provider).supports_reasoning_effort


def apply_resolved_provider(agent: Agent, resolved: ResolvedProvider) -> bool:
    """Copy runtime settings from a resolved provider onto an active agent.

    Returns whether any field actually changed. Does not touch session state.
    Re-derives model capability fields from the resolved model config when the
    provider or model changes so the agent reports accurate support flags.
    """

    runtime_changed = (
        agent.provider != resolved.provider
        or agent.model != resolved.model
        or agent.api_base != resolved.api_base
        or agent.api_key != resolved.api_key
        or agent.reasoning_effort != resolved.reasoning_effort
    )

    agent.provider = resolved.provider
    agent.model = resolved.model
    agent.api_key = resolved.api_key
    agent.api_base = resolved.api_base
    agent.reasoning_effort = resolved.reasoning_effort

    if runtime_changed:
        model_config = resolved.model_config
        agent.refresh_capabilities(
            max_tokens=model_config.max_output_tokens if model_config else None,
            context_window=model_config.context_window if model_config else None,
            supports_reasoning=model_config.supports_reasoning if model_config else None,
            supports_image_input=model_config.supports_image_input if model_config else None,
            supports_pdf_input=model_config.supports_pdf_input if model_config else None,
        )
    return runtime_changed


class TerminalChat:
    """Own the interactive TUI session, including slash commands and rendering."""

    def __init__(
        self,
        *,
        agent: Agent,
        settings: Settings,
        store: SessionStore,
        session_id: str,
        view: TerminalView | None = None,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.store = store
        self.session_id = session_id
        self.view = view or TerminalView()
        self._current_renderer: ReplyRenderer | None = None
        self.prompt_session: PromptSession[str] = PromptSession(
            history=FileHistory(history_file_path()),
            completer=_PromptCompleter(cwd=self.agent.cwd),
            key_bindings=_build_chat_key_bindings(),
            multiline=True,
            prompt_continuation="  ",
        )
        self.agent.hooks = build_permission_hooks(self.settings, review=self._review_tool_call)

    async def _review_tool_call(self, request: ToolReviewRequest) -> ToolReviewDecision:
        if self._current_renderer is not None:
            self._current_renderer.prepare_interaction()
        self.view.console.print()
        title = Text()
        title.append(f"{TOOL_MARKER} Review", style=WARNING)
        title.append(f"  {request.tool_name.capitalize()}")
        self.view.console.print(title)
        if request.preview:
            preview = request.preview.replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:119] + "…"
            self.view.console.print(Text(f"  {preview}", style=MUTED))
        selected = await choose([("allow", "Allow"), ("deny", "Deny")], default="allow")
        if selected == "allow":
            return "allow"
        self.agent.cancel()
        return "deny"

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
            self._current_renderer = renderer
            user_message = self._build_user_message(user_input)
            try:
                await renderer.render(self.agent, user_message)
            except (KeyboardInterrupt, asyncio.CancelledError):
                self.agent.cancel()
                renderer.cancel()
                # Python 3.11+: uncancel the task so the loop can continue after Ctrl+C.
                task = asyncio.current_task()
                if task is not None:
                    with suppress(AttributeError):
                        task.uncancel()
            finally:
                self._current_renderer = None

    def _build_user_message(self, text: str) -> ConversationMessage:
        """Build one user message with skill snapshots and `@path` attachments."""

        blocks: list[dict[str, Any]] = [*build_skill_snapshot_blocks(text, self.agent.cwd), text_block(text)]
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

            img = detect_image_mime_type(path)
            pdf = detect_document_mime_type(path)
            # An image/PDF the model can't ingest becomes a text placeholder so the
            # message still references it.
            if img and not self.agent.supports_image_input:
                blocks.append(unsupported_attachment_block(name=path_text, mime_type=img, kind="image", path=path_text))
                continue
            if pdf and not self.agent.supports_pdf_input:
                blocks.append(
                    unsupported_attachment_block(name=path_text, mime_type=pdf, kind="document", path=path_text)
                )
                continue
            # Text snippets keep the resolved path as the visible name; image/PDF default to the basename.
            # A non-UTF-8 binary raises ValueError inside build_attachment_blocks and is skipped.
            name = None if img or pdf else path_text
            try:
                blocks.extend(build_attachment_blocks([Attachment.path(path_text, name=name)], cwd=self.agent.cwd))
            except ValueError:
                continue

        return build_message("user", blocks)

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
        command = resolve_slash_command(command) or command

        match command:
            case "/q":
                self.view.console.print("[dim]bye[/dim]")
                return "exit"
            case "/c" | "/clear":
                await self.store.clear_session(self.session_id)
                self.agent.clear()
                self.view.console.print(f"[green]{TOOL_MARKER}[/green] [dim]cleared[/dim]")
            case "/compact":
                if argument:
                    # `/compact <text>` is not a command; send it as user text.
                    return False
                await self._compact_session()
            case "/new":
                self._start_new_session()
            case "/rewind":
                prefill = await self._rewind()
                if prefill:
                    return prefill
            case "/resume":
                await self._resume_session()
            case "/provider":
                if argument:
                    self._apply_provider_change(argument)
                else:
                    await self._switch_provider()
            case "/model":
                if argument:
                    self._apply_model_change(argument)
                else:
                    await self._switch_model()
            case "/effort":
                if argument:
                    self._apply_effort_change(argument)
                else:
                    await self._switch_effort()
            case _:
                return False

        return True

    def _print_help(self) -> None:
        self.view.console.print()
        for _command, usage, desc in _COMMANDS:
            line = Text()
            line.append(f"  {usage:<20}", style="bold")
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

    async def _compact_session(self) -> None:
        """Compact the conversation now and print the ``compacted`` divider."""

        try:
            with self.view.console.status(Text("Compacting…", style=MUTED), spinner="dots"):
                await self.agent.acompact()
        except NothingToCompactError:
            self.view.console.print(Text("nothing to compact", style=MUTED))
            return
        except (KeyboardInterrupt, asyncio.CancelledError):
            self.agent.cancel()
            # Python 3.11+: uncancel the task so the loop can continue after Ctrl+C.
            task = asyncio.current_task()
            if task is not None:
                with suppress(AttributeError):
                    task.uncancel()
            self.view.console.print(Text("cancelled", style=MUTED))
            return
        except Exception as exc:
            text = Text(f"{ERROR_MARKER} ", style=ERROR)
            text.append(str(exc), style=ERROR)
            self.view.console.print(text)
            return
        self.view.print_compact_marker()

    def _start_new_session(self) -> None:
        """Start a fresh session while keeping the current runtime settings."""

        self.session_id = uuid4().hex
        self.agent = clone_agent(self.agent, store=self.store, session_id=self.session_id)
        self.view.print_header(
            provider=self.agent.provider,
            model=self.agent.model,
            session={"id": self.session_id, "title": "New chat"},
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

        # Collect real user text turns, skipping tool-result-only and attachment
        # blocks via the shared flattener (same view as the history preview).
        user_turns: dict[int, str] = {}  # message_index -> text
        for i, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            text = flatten_message_text(msg, include_thinking=False)
            if text:
                user_turns[i] = text

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

        original_text = user_turns[selected]

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

        self.session_id = str(session["id"])
        data = await self.store.load_session(self.session_id)
        if data is None:
            self.view.console.print("[red]failed to load session[/red]")
            return
        messages = data["messages"]
        loaded_session = data["session"]
        self.agent = clone_agent(self.agent, store=self.store, session_id=self.session_id)
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

        options = list_provider_options(self.settings)
        current = get_provider_option(self.settings, provider=self.agent.provider, api_base=self.agent.api_base)

        choices: list[tuple[str, str]] = []
        for option in options:
            models = "  ".join(option.models[:3])
            if len(option.models) > 3:
                models += f"  +{len(option.models) - 3}"
            choices.append((option.name, f"{option.name}  {models}"))

        selected = await choose(choices, default=current.name if current else None)
        if selected is not None:
            self._apply_provider_change(selected)

    async def _switch_model(self) -> None:
        """Prompt for a model supported by the current provider runtime."""

        models = list_model_options(
            self.settings,
            provider=self.agent.provider,
            api_base=self.agent.api_base,
            current_model=self.agent.model,
        )
        choices = [(m, m) for m in models]
        selected = await choose(choices, default=self.agent.model)
        if selected is not None:
            self._apply_model_change(selected)

    def _apply_provider_change(self, provider_name: str) -> None:
        """Switch the active provider, keeping session history unchanged."""

        self.settings = get_settings(self.agent.cwd)
        try:
            resolved = resolve_provider(self.settings, provider_name=provider_name)
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        changed = apply_resolved_provider(self.agent, resolved)
        label = f"{self.agent.provider} / {self.agent.model}"
        if self.agent.reasoning_effort:
            label += f" [effort: {self.agent.reasoning_effort}]"
        self._print_runtime_status("provider/model", label, changed=changed)

    def _apply_model_change(self, model_name: str) -> None:
        """Switch the active model for the current provider runtime."""

        self.settings = get_settings(self.agent.cwd)
        current = get_provider_option(self.settings, provider=self.agent.provider, api_base=self.agent.api_base)
        provider_name = current.name if current else self.agent.provider
        try:
            resolved = resolve_provider(self.settings, provider_name=provider_name, model=model_name)
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        changed = apply_resolved_provider(self.agent, resolved)
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

        try:
            resolved = normalize_reasoning_effort(effort)
        except ValueError as exc:
            self.view.console.print(f"[red]{exc}[/red]")
            return

        changed = resolved != self.agent.reasoning_effort
        self.agent.reasoning_effort = resolved
        self._print_runtime_status("effort", resolved or "default", changed=changed)
