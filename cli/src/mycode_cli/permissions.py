"""CLI permission policy built on SDK tool hooks.

The shell classification is a heuristic allowlist, not a sandbox: it reads the
command text, and ``readonly`` means only that the command matches programs and
flags known not to write or execute anything else. A list or pipeline (``|``,
``||``, ``&&``, ``;``) is ``readonly`` when every command in it is; ``2>&1``
writes nothing and is ignored.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from mycode import Hooks, ToolExecutionResult, ToolHookContext
from mycode.tools import ToolSpec
from mycode_cli.config import PermissionConfig, PermissionLevel, Settings
from mycode_cli.system_prompt import Skill
from mycode_cli.workspace import CliDeps, resolve_path

PermissionDecision = Literal["allow", "ask", "deny"]
ToolReviewDecision = Literal["allow", "deny"]

PERMISSION_DENIED_OUTPUT = "error: permission denied"
PERMISSION_DENIED_BY_USER_OUTPUT = "error: permission denied by user"

_LEVEL_RANK: dict[PermissionLevel, int] = {"readonly": 0, "safe": 1, "standard": 2, "yolo": 3}

# The lowest tier ``classify_tool`` gives a call of each tool; other tools are ``yolo``.
_TOOL_LOWEST_LEVEL: dict[str, PermissionLevel] = {
    "read": "readonly",
    "bash": "readonly",
    "write": "safe",
    "edit": "safe",
    "agent": "safe",
    "webfetch": "standard",
    "websearch": "standard",
}

# An unquoted ``2>&1`` word only merges stderr into stdout.
_STDERR_TO_STDOUT = re.compile(r"2>&1(?=[\s|;&]|$)")

# shlex's default punctuation characters. A token made only of them is a shell
# operator: a list, pipe, redirection, or (process) substitution.
_SHELL_PUNCTUATION = frozenset("();<>|&")

_DANGEROUS_PROGRAMS = {
    "rm", "rmdir", "mv", "cp", "sudo", "chmod", "chown",
    "kill", "pkill", "dd", "mkfs", "mount", "umount", "shutdown", "reboot",
}  # fmt: skip
_DANGEROUS_GIT_SUBCOMMANDS = {"reset", "clean", "checkout", "restore"}

# awk/sed are excluded: both can write files and shell out, and statically
# parsing their scripts isn't worth it.
_READONLY_PROGRAMS = {
    "pwd", "ls", "dir", "tree", "rg", "grep", "cat", "head", "tail",
    "wc", "stat", "file", "du", "df", "which", "printenv",
    "date", "uname", "whoami", "id", "hostname", "ps", "uptime",
    "realpath", "dirname", "basename",
    "sort", "uniq", "cut", "tr",
}  # fmt: skip
_READONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "blame", "describe"}
_READONLY_BRANCH_FLAGS = {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--show-current"}

# find flags that write files or execute commands.
_FIND_DANGEROUS_FLAGS = {
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprint0", "-fprintf", "-fls",
}  # fmt: skip


class PermissionCheck(NamedTuple):
    tier: PermissionLevel
    preview: str


@dataclass(frozen=True)
class ToolReviewRequest:
    tool_call_id: str
    tool_name: str
    preview: str
    permission: PermissionConfig
    # The description of the subagent whose tool call this is; ``None`` for the main agent.
    agent: str | None = None


ToolReviewCallback = Callable[[ToolReviewRequest], Awaitable[ToolReviewDecision]]


def build_permission_hooks(
    settings: Settings,
    *,
    skills: list[Skill],
    review: ToolReviewCallback | None = None,
) -> Hooks:
    """Build the ``before_tool`` hook; reads inside the ``skills`` directories are ``readonly``."""

    hooks = Hooks()
    skill_roots = [Path(skill.path).parent for skill in skills]

    @hooks.before_tool
    async def check_permission(ctx: ToolHookContext[CliDeps]) -> ToolExecutionResult | None:
        check = classify_tool(ctx, project=settings.project, skill_roots=skill_roots)
        decision = permission_decision(settings.permission, check.tier)
        if decision == "allow":
            return None

        if decision == "ask" and review is not None:
            review_decision = await review(
                ToolReviewRequest(
                    tool_call_id=ctx.tool_call_id,
                    tool_name=ctx.tool_name,
                    preview=check.preview,
                    permission=settings.permission,
                )
            )
            if review_decision == "allow":
                return None
            return ToolExecutionResult(output=PERMISSION_DENIED_BY_USER_OUTPUT, is_error=True)

        return ToolExecutionResult(
            output=(
                f"{PERMISSION_DENIED_OUTPUT}: {ctx.tool_name} call needs level {check.tier}, "
                f"this agent allows {settings.permission.level}"
            ),
            is_error=True,
        )

    return hooks


def permission_decision(permission: PermissionConfig, tier: PermissionLevel) -> PermissionDecision:
    if _LEVEL_RANK[tier] <= _LEVEL_RANK[permission.level]:
        return "allow"
    return permission.mode


def narrow_permission(permission: PermissionConfig, limit: PermissionConfig) -> PermissionConfig:
    """The stricter of the two: the lower level, and ``deny`` when either denies."""

    level = min(permission.level, limit.level, key=_LEVEL_RANK.__getitem__)
    return PermissionConfig(level=level, mode="deny" if "deny" in (permission.mode, limit.mode) else "ask")


def usable_tools(tools: Sequence[ToolSpec], permission: PermissionConfig) -> list[ToolSpec]:
    """``tools`` without those whose every call ``permission`` denies; in ``ask`` mode any call can be approved."""

    if permission.mode == "ask":
        return list(tools)
    rank = _LEVEL_RANK[permission.level]
    return [spec for spec in tools if _LEVEL_RANK[_TOOL_LOWEST_LEVEL.get(spec.name, "yolo")] <= rank]


def classify_tool(
    ctx: ToolHookContext[CliDeps],
    *,
    project: str,
    skill_roots: list[Path],
) -> PermissionCheck:
    name = ctx.tool_name.lower()

    if name == "bash":
        command = str(ctx.tool_input.get("command") or "").strip()
        preview = f"{command} (background)" if ctx.tool_input.get("background") else command
        return PermissionCheck(_classify_bash(command), preview)

    if name == "agent":
        # Starting a subagent has no effect of its own; each of its tool calls is checked.
        return PermissionCheck("safe", str(ctx.tool_input.get("description") or "").strip())

    if name == "webfetch":
        url = str(ctx.tool_input.get("url") or "").strip()
        return PermissionCheck("standard", url)

    if name == "websearch":
        query = str(ctx.tool_input.get("query") or "").strip()
        return PermissionCheck("standard", query)

    if name in {"read", "write", "edit"}:
        raw = str(ctx.tool_input.get("path") or "")
        path = resolve_path(raw, cwd=ctx.deps.cwd)
        project_path = Path(project).resolve(strict=False)
        preview = raw or str(path)
        if name == "read" and (
            path.is_relative_to(ctx.deps.tool_output_dir.resolve(strict=False))
            or any(path.is_relative_to(root) for root in skill_roots)
        ):
            return PermissionCheck("readonly", preview)
        if not path.is_relative_to(project_path):
            return PermissionCheck("yolo", preview)
        return PermissionCheck("readonly" if name == "read" else "safe", preview)

    return PermissionCheck("yolo", ctx.tool_name)


def _classify_bash(command: str) -> PermissionLevel:
    segments = _split_list(command)
    if len(segments) > 1:
        # A list or pipeline of readonly commands only reads; anything else in it needs yolo.
        return "readonly" if all(_classify_command(segment) == "readonly" for segment in segments) else "yolo"
    return _classify_command(segments[0])


def _split_list(command: str) -> list[str]:
    """Split ``command`` at each ``|``, ``||``, ``&&`` and ``;`` outside quotes and
    escapes, and drop each unquoted ``2>&1`` word.

    ``|&``, ``&``, and repeated operators stay in a segment or leave an empty
    one, which ``_classify_command`` rejects.
    """

    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "|;" or command.startswith("&&", index):
            segments.append("".join(current))
            current = []
            index += 2 if command.startswith(("||", "&&"), index) else 1
            continue
        elif (not current or current[-1].isspace()) and _STDERR_TO_STDOUT.match(command, index):
            index += len("2>&1")
            continue
        current.append(char)
        index += 1
    segments.append("".join(current))
    return segments


def _classify_command(command: str) -> PermissionLevel:
    command = command.strip()
    if not command:
        return "yolo"
    # Newlines and shell expansion bypass static analysis; treat as compound.
    if "\n" in command or "\r" in command or "$(" in command or "`" in command:
        return "yolo"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        words = shlex.split(command, posix=True)
    except ValueError:
        return "yolo"
    if not words or any(set(t) <= _SHELL_PUNCTUATION for t in tokens):
        return "yolo"

    program = Path(words[0]).name
    if _is_dangerous(program, words):
        return "yolo"
    if _is_readonly(program, words):
        return "readonly"
    return "standard"


def _is_dangerous(program: str, words: list[str]) -> bool:
    if program in _DANGEROUS_PROGRAMS:
        return True
    if program == "sed" and any(w == "-i" or w.startswith("-i") for w in words[1:]):
        return True
    if program == "find" and any(w in _FIND_DANGEROUS_FLAGS for w in words[1:]):
        return True
    if program != "git":
        return False
    sub = _git_subcommand(words)
    if sub in _DANGEROUS_GIT_SUBCOMMANDS:
        return True
    return sub == "push" and any(w in {"-f", "--force", "--force-with-lease"} for w in words[2:])


def _is_readonly(program: str, words: list[str]) -> bool:
    if len(words) == 2 and words[1] in {"--version", "-v", "version"}:
        return True
    if program == "env":
        # With arguments env runs a command or changes the environment for one.
        return len(words) == 1
    if program == "sort":
        # -o/--output writes a file; any long option starting --o abbreviates --output.
        return not any(w.startswith("--o") or (_is_short_flags(w) and "o" in w) for w in words[1:])
    if program == "uniq":
        # A second file operand is the output file; after ``--`` every word is an operand.
        args = words[1:]
        end = args.index("--") if "--" in args else len(args)
        operands = [w for w in args[:end] if w == "-" or not w.startswith("-")] + args[end + 1 :]
        return len(operands) < 2
    if program == "rg":
        # --pre and --pre-glob run a preprocessor command on each file.
        return not any(w.split("=", 1)[0] in {"--pre", "--pre-glob"} for w in words[1:])
    if program in _READONLY_PROGRAMS:
        return True
    if program == "find":
        return True
    if program == "command" and words[1:2] == ["-v"] and len(words) >= 3:
        return True
    if program == "type" and len(words) >= 2:
        return True
    if program != "git":
        return False
    sub = _git_subcommand(words)
    if sub in _READONLY_GIT_SUBCOMMANDS:
        return True
    if sub == "remote":
        return not any(w in {"add", "remove", "rm", "rename", "set-url"} for w in words[2:])
    if sub == "branch":
        return all(w in _READONLY_BRANCH_FLAGS for w in words[2:])
    return False


def _is_short_flags(word: str) -> bool:
    return word.startswith("-") and not word.startswith("--")


def _git_subcommand(words: list[str]) -> str | None:
    i = 1
    while i < len(words):
        w = words[i]
        if w in {"-C", "-c", "--git-dir", "--work-tree"}:
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w
    return None


__all__ = [
    "PERMISSION_DENIED_BY_USER_OUTPUT",
    "PERMISSION_DENIED_OUTPUT",
    "ToolReviewCallback",
    "ToolReviewDecision",
    "ToolReviewRequest",
    "build_permission_hooks",
    "classify_tool",
    "permission_decision",
]
