"""Interactive terminal chat for the CLI."""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, override
from uuid import uuid4

from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from rich.spinner import Spinner
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
from mycode_cli.config import (
    ResolvedProvider,
    Settings,
    get_settings,
    normalize_reasoning_effort,
    provider_models,
    resolve_mycode_home,
    resolve_provider,
    resolve_provider_choices,
)
from mycode_cli.permissions import ToolReviewDecision, ToolReviewRequest, build_permission_hooks
from mycode_cli.runtime import load_session_cost
from mycode_cli.sessions import SessionStore
from mycode_cli.system_prompt import build_skill_snapshot_blocks, discover_slash_skills
from mycode_cli.workspace import CliDeps, resolve_path

from .render import (
    TurnRenderer,
    compact_marker,
    error_line,
    format_local_timestamp,
    header_lines,
    history_preview,
    shorten,
    tool_label,
    user_echo,
)
from .state import load_efforts, save_efforts
from .terminal import Terminal
from .theme import MUTED, SUCCESS, TOOL_MARKER, WARNING

_COMMANDS = (
    ("/clear", "Clear conversation"),
    ("/compact", "Compact conversation context"),
    ("/new", "New session"),
    ("/resume", "Switch session"),
    ("/rewind", "Rewind to a previous message"),
    ("/model", "Switch model"),
    ("/effort", "Set reasoning effort"),
    ("/q", "Quit"),
)
_SLASH_COMMANDS = tuple(command for command, _ in _COMMANDS)
# Only treat `@path` as a reference when it starts a standalone token.
_AT_PATH_RE = re.compile(r"""(?<!\S)@(?:'(?P<single>[^']*)'?$|"(?P<double>[^"]*)"?$|(?P<plain>[^\s'"]*))$""")
_SKILL_TOKEN_RE = re.compile(r"(?<!\S)/(?P<name>[a-zA-Z0-9_-]*)$")


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
            for cmd, desc in _COMMANDS:
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
            base_dir = resolve_path(query or ".", cwd=cwd)
        else:
            head, sep, tail = query.rpartition("/")
            base_prefix = f"{head}{sep}" if sep else ""
            partial = tail if sep else query
            base_dir = resolve_path(base_prefix or ".", cwd=cwd)

        if not base_dir.is_dir():
            return
        entries = [(entry, entry.is_dir()) for entry in base_dir.iterdir() if entry.name.startswith(partial)]
        for entry, is_dir in sorted(entries, key=lambda item: (not item[1], item[0].name.lower())):
            candidate = f"{base_prefix}{entry.name}{'/' if is_dir else ''}"
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
                display_meta="dir" if is_dir else "file",
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
    """Build the chat-specific input key bindings: Enter completion handling and paste rewriting."""

    kb = KeyBindings()

    # Enter accepts an open completion before it submits.
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


def clone_agent(agent: Agent, *, store: SessionStore, session_id: str, cwd: str) -> Agent:
    """Keep the current runtime config while swapping session state.

    History auto-loads from disk when ``session_id`` exists under the store.
    """

    return Agent(
        model=agent.model,
        provider=agent.provider,
        session_dir=store.data_dir,
        session_id=session_id,
        api_key=agent.api_key,
        api_base=agent.api_base,
        max_turns=agent.max_turns,
        max_tokens=agent.max_tokens,
        context_window=agent.context_window,
        compact_threshold=agent.compact_threshold,
        reasoning_effort=agent.reasoning_effort,
        supports_reasoning_effort=agent.supports_reasoning_effort,
        supports_image_input=agent.supports_image_input,
        supports_pdf_input=agent.supports_pdf_input,
        system=agent.system,
        tools=agent.tools.specs,
        hooks=agent.hooks,
        deps=CliDeps.for_session(cwd=cwd, data_dir=store.data_dir, session_id=session_id),
    )


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
    agent.supports_reasoning_effort = resolved.supports_reasoning_effort

    if runtime_changed:
        model_config = resolved.model_config
        agent.refresh_capabilities(
            max_tokens=model_config.max_output_tokens if model_config else None,
            context_window=model_config.context_window if model_config else None,
            supports_image_input=model_config.supports_image_input if model_config else None,
            supports_pdf_input=model_config.supports_pdf_input if model_config else None,
        )
    return runtime_changed


class TerminalChat:
    """Own the interactive TUI session: slash commands, session switching, and agent turns."""

    def __init__(
        self,
        *,
        agent: Agent,
        settings: Settings,
        store: SessionStore,
        session_id: str,
        provider_name: str | None = None,
        reasoning_efforts: tuple[str, ...] = (),
        session: dict[str, Any] | None = None,
        mode: Literal["new", "resumed"] = "new",
        messages: list[ConversationMessage] | None = None,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.store = store
        self.session_id = session_id
        self.provider_name = provider_name or agent.provider
        self.reasoning_efforts = reasoning_efforts
        self.effort_preferences = load_efforts()
        self._restore_effort()
        self._session = session or {}
        self._mode: Literal["new", "resumed"] = mode
        self._messages = messages or []
        self.terminal = Terminal(
            history_path=history_file_path(),
            completer=_PromptCompleter(cwd=self.settings.cwd),
            key_bindings=_build_chat_key_bindings(),
        )
        self.agent.hooks = build_permission_hooks(self.settings, review=self._review_tool_call)

    async def _review_tool_call(self, request: ToolReviewRequest) -> ToolReviewDecision:
        title = Text()
        title.append(f"{TOOL_MARKER} Review", style=WARNING)
        title.append(f"  {tool_label(request.tool_name)}")
        lines: list[Text] = [Text(), title]
        if request.preview:
            lines.append(Text(f"  {shorten(request.preview, self.terminal.width - 2)}", style=MUTED))
        self.terminal.print(*lines)
        selected = await self.terminal.choose([("allow", "Allow"), ("deny", "Deny")], default="allow")
        if selected == "allow":
            return "allow"
        self.agent.cancel()
        return "deny"

    async def run(self) -> None:
        """Run the interactive chat until the user exits the terminal UI."""

        await self.terminal.run(self._main)

    async def _main(self) -> None:
        self._print_header(self._session, mode=self._mode, message_count=len(self._messages))
        if self._mode == "resumed":
            self._print_history(self._messages)

        while True:
            try:
                user_input = (await self.terminal.read()).strip()
            except EOFError:
                self.terminal.print(Text(), Text("bye", style=MUTED))
                return
            if not user_input:
                continue
            self.terminal.print(Text(), user_echo(user_input))

            result = await self._handle_command(user_input)
            if result == "exit":
                return
            if isinstance(result, str):
                # The command prefills the next input (e.g. /rewind).
                self.terminal.set_input(result)
                continue
            if result:
                continue

            self.terminal.print()
            await self._run_turn(user_input)

    async def _run_turn(self, user_input: str) -> None:
        """Send one user message and render the agent's turn; Esc or Ctrl+C cancels it."""

        # Fold the session JSONL fresh each turn: covers resume, /clear,
        # /new, /rewind, and manual /compact without tracking state.
        session_cost = await load_session_cost(self.store, self.session_id)
        renderer = TurnRenderer(
            self.terminal,
            model=self.agent.model,
            context_window=self.agent.context_window,
            session_cost_base=session_cost,
        )
        user_message = self._build_user_message(user_input)
        await self.store.record_user_turn(self.session_id, cwd=self.settings.cwd, text=user_input)
        self.terminal.busy = True
        self.terminal.on_cancel = self.agent.cancel
        try:
            await renderer.render(self.agent, user_message)
        finally:
            self.terminal.busy = False
            self.terminal.on_cancel = None

    def _print_header(self, session: dict[str, Any], *, mode: str, message_count: int) -> None:
        self.terminal.print(
            Text(),
            *header_lines(
                provider=self.provider_name,
                model=self.agent.model,
                session=session,
                mode=mode,
                message_count=message_count,
                reasoning_effort=self.agent.reasoning_effort,
            ),
        )

    def _print_history(self, messages: list[ConversationMessage]) -> None:
        if preview := history_preview(messages, width=self.terminal.width):
            self.terminal.print(*preview)

    def _build_user_message(self, text: str) -> ConversationMessage:
        """Build one user message with skill snapshots and `@path` attachments."""

        blocks: list[dict[str, Any]] = [*build_skill_snapshot_blocks(text, self.settings.cwd), text_block(text)]
        try:
            tokens = shlex.split(text.replace("\r\n", "\n").replace("\r", "\n"), posix=True)
        except ValueError:
            return build_message("user", blocks)

        seen: set[str] = set()
        for token in tokens:
            if not token.startswith("@") or token == "@":
                continue
            path = resolve_path(token[1:], cwd=self.settings.cwd)
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
                blocks.extend(build_attachment_blocks([Attachment.path(path_text, name=name)]))
            except ValueError:
                continue

        return build_message("user", blocks)

    async def _handle_command(self, text: str) -> str | bool:
        """Handle a slash command. Returns "exit" to quit, True if consumed, False otherwise."""

        # Non-slash exit aliases.
        if text in ("exit", "quit"):
            self.terminal.print(Text("bye", style=MUTED))
            return "exit"

        if not text.startswith("/"):
            return False

        command, _, argument = text.partition(" ")
        argument = argument.strip()
        command = resolve_slash_command(command) or command

        match command:
            case "/q":
                self.terminal.print(Text("bye", style=MUTED))
                return "exit"
            case "/c" | "/clear":
                await self.store.clear_session(self.session_id)
                self.agent.clear()
                self._print_done("cleared")
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
            case "/model":
                await self._switch_model(argument)
            case "/effort":
                if argument:
                    self._apply_effort_change(argument)
                else:
                    await self._switch_effort()
            case _:
                return False

        return True

    def _print_done(self, action: str, value: str = "") -> None:
        """Print a ``⏺ action value`` confirmation."""

        text = Text(f"{TOOL_MARKER} ", style=SUCCESS)
        text.append(action, style=MUTED)
        if value:
            text.append(f" {value}")
        self.terminal.print(text)

    def _print_runtime_status(self, action: str, value: str, *, changed: bool) -> None:
        """Print the result of a runtime-only change."""

        if changed:
            self._print_done(f"{action} →", value)
        else:
            self._print_done("already using", value)

    def _supports_effort_or_warn(self) -> bool:
        """Return whether the current model supports reasoning effort."""

        if self.agent.supports_reasoning_effort and self.reasoning_efforts:
            return True
        self.terminal.print(Text("current model does not support reasoning effort", style=MUTED))
        return False

    async def _compact_session(self) -> None:
        """Compact the conversation now and print the ``compacted`` divider."""

        self.terminal.busy = True
        self.terminal.on_cancel = self.agent.cancel
        self.terminal.set_tail(Spinner("dots", text=Text("Compacting…", style=MUTED), style=MUTED))
        try:
            await self.agent.acompact()
        except NothingToCompactError:
            self.terminal.print(Text("nothing to compact", style=MUTED))
            return
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # The agent reports a user stop (Esc / Ctrl+C) as CancelledError.
            self.terminal.print(Text("cancelled", style=MUTED))
            return
        except Exception as exc:
            self.terminal.print(error_line(str(exc)))
            return
        finally:
            self.terminal.set_tail(None)
            self.terminal.busy = False
            self.terminal.on_cancel = None
        await self.store.touch(self.session_id)
        self.terminal.print(compact_marker())

    def _start_new_session(self) -> None:
        """Start a fresh session while keeping the current runtime settings."""

        self.session_id = uuid4().hex
        self.agent = clone_agent(self.agent, store=self.store, session_id=self.session_id, cwd=self.settings.cwd)
        self._print_header({"id": self.session_id, "title": "New chat"}, mode="new", message_count=0)

    async def _rewind(self) -> str | None:
        """Rewind the conversation to a chosen user message.

        Shows an interactive selector of all real user text messages.
        Selecting one truncates the in-memory conversation to the slice before
        that user message index and appends a rewind marker to the session log.
        Returns the original message text to prefill the next prompt.
        """
        messages = self.agent.messages
        if not messages:
            self.terminal.print(Text("nothing to rewind", style=MUTED))
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
            self.terminal.print(Text("no user messages to rewind to", style=MUTED))
            return None

        # Build selector options — most recent first.
        options: list[tuple[int, str]] = []
        for msg_index, text in reversed(list(user_turns.items())):
            options.append((msg_index, shorten(text, 60)))

        selected = await self.terminal.choose(options)
        if selected is None:
            return None

        original_text = user_turns[selected]

        # Persist the rewind event and truncate in-memory messages.
        await self.store.append_rewind(self.session_id, selected)
        await self.store.touch(self.session_id)
        self.agent.messages = messages[:selected]

        self._print_done("rewound")
        if self.agent.messages:
            self._print_history(self.agent.messages)
        else:
            self.terminal.print(Text("conversation is now empty", style=MUTED))

        return original_text

    async def _resume_session(self) -> None:
        """Switch to another saved session in the current workspace."""

        sessions = await self.store.list_sessions(cwd=self.settings.cwd)
        sessions = [s for s in sessions if s.get("id") != self.session_id]
        if not sessions:
            self.terminal.print(Text("no other sessions in this workspace", style=MUTED))
            return

        options: list[tuple[dict[str, Any], str]] = []
        for s in sessions:
            title = shorten(str(s.get("title") or "New chat"), 40)
            ts = format_local_timestamp(str(s.get("updated_at") or ""), "%m-%d %H:%M")
            label = f"{title}  {ts}" if ts else title
            options.append((s, label))

        session = await self.terminal.choose(options)
        if session is None:
            return

        self.session_id = str(session["id"])
        data = await self.store.load_session(self.session_id)
        if data is None:
            self.terminal.print(error_line("failed to load session"))
            return
        messages = data["messages"]
        self.agent = clone_agent(self.agent, store=self.store, session_id=self.session_id, cwd=self.settings.cwd)
        self._print_header(data["session"], mode="resumed", message_count=len(messages))
        self._print_history(messages)

    async def _switch_model(self, query: str) -> None:
        """Pick a model from every available provider and apply it to the active agent.

        A query naming a listed model exactly switches to it directly, preferring
        the current provider; any other query opens the picker filtered by it.
        """

        self.settings = get_settings(self.settings.cwd)
        current = (self.provider_name, self.agent.model)
        groups: list[tuple[str, list[str]]] = []
        for provider in resolve_provider_choices(self.settings):
            name = provider.provider_name or provider.provider
            models = provider_models(self.settings, provider)
            if name == self.provider_name and self.agent.model not in models:
                models.append(self.agent.model)
            groups.append((name, models))

        matches = [(name, model) for name, models in groups for model in models if model == query]
        if matches:
            selected = current if current in matches else matches[0]
        else:
            options: list[tuple[tuple[str, str], Text] | str] = []
            for name, models in groups:
                options.append(name)
                for model in models:
                    label = Text(model)
                    if (name, model) == current:
                        label.append("  current", style=MUTED)
                    options.append(((name, model), label))
            selected = await self.terminal.choose(options, default=current, query=query)
            if selected is None:
                return

        provider_name, model = selected
        try:
            resolved = resolve_provider(self.settings, provider_name=provider_name, model=model)
        except ValueError as exc:
            self.terminal.print(error_line(str(exc)))
            return

        changed = apply_resolved_provider(self.agent, resolved)
        self.provider_name = provider_name
        self.reasoning_efforts = resolved.reasoning_efforts
        self._restore_effort()
        label = f"{provider_name} / {model}"
        if self.agent.reasoning_effort:
            label += f" [effort: {self.agent.reasoning_effort}]"
        self._print_runtime_status("model", label, changed=changed)

    async def _switch_effort(self) -> None:
        """Prompt for a reasoning effort level."""

        if not self._supports_effort_or_warn():
            return

        current = self.agent.reasoning_effort or "auto"
        choices = [(effort, effort) for effort in ("auto", *self.reasoning_efforts)]
        selected = await self.terminal.choose(choices, default=current)
        if selected is not None:
            self._apply_effort_change(selected)

    def _apply_effort_change(self, effort: str) -> None:
        """Apply a reasoning effort change to the active agent."""

        if not self._supports_effort_or_warn():
            return

        try:
            resolved = normalize_reasoning_effort(effort)
        except ValueError as exc:
            self.terminal.print(error_line(str(exc)))
            return

        if resolved is not None and resolved not in self.reasoning_efforts:
            supported = ", ".join(self.reasoning_efforts)
            message = f"reasoning effort {resolved!r} is not supported by model {self.agent.model!r}"
            message += f"; supported efforts: {supported}"
            self.terminal.print(error_line(message))
            return

        changed = resolved != self.agent.reasoning_effort
        self.agent.reasoning_effort = resolved
        self.effort_preferences[self._effort_key()] = resolved or "auto"
        save_efforts(self.effort_preferences)
        self._print_runtime_status("effort", resolved or "auto", changed=changed)

    def _effort_key(self) -> str:
        return f"{self.provider_name}/{self.agent.model}"

    def _restore_effort(self) -> None:
        saved = self.effort_preferences.get(self._effort_key())
        if saved is None or saved == "auto":
            self.agent.reasoning_effort = None
            return
        if self.agent.supports_reasoning_effort and saved in self.reasoning_efforts:
            self.agent.reasoning_effort = saved
            return
        del self.effort_preferences[self._effort_key()]
        save_efforts(self.effort_preferences)
        self.agent.reasoning_effort = None
