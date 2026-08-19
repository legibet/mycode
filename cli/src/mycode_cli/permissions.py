"""CLI permission policy built on SDK tool hooks."""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from mycode import Hooks, ToolExecutionResult, ToolHookContext
from mycode_cli.config import PermissionConfig, PermissionLevel, Settings
from mycode_cli.system_prompt import discover_skills
from mycode_cli.workspace import CliDeps, resolve_path

PermissionTier = Literal["readonly", "safe", "standard", "yolo"]
PermissionDecision = Literal["allow", "ask", "deny"]
ToolReviewDecision = Literal["allow", "deny"]

PERMISSION_DENIED_OUTPUT = "error: permission denied"
PERMISSION_DENIED_BY_USER_OUTPUT = "error: permission denied by user"

_LEVEL_RANK: dict[PermissionLevel, int] = {"readonly": 0, "safe": 1, "standard": 2, "yolo": 3}

_SHELL_CONTROL_TOKENS = {
    "&&", "||", ";", "|", "|&", "&",
    ">", ">>", ">|", "&>", ">&",
    "<", "<<", "<<<", "<&",
}  # fmt: skip

_DANGEROUS_PROGRAMS = {
    "rm", "rmdir", "mv", "cp", "sudo", "chmod", "chown",
    "kill", "pkill", "dd", "mkfs", "mount", "umount", "shutdown", "reboot",
}  # fmt: skip
_DANGEROUS_GIT_SUBCOMMANDS = {"reset", "clean", "checkout", "restore"}

# awk/sed are excluded: both can write files and shell out, and statically
# parsing their scripts isn't worth it.
_READONLY_PROGRAMS = {
    "pwd", "ls", "dir", "tree", "rg", "grep", "cat", "head", "tail",
    "wc", "stat", "file", "du", "df", "which", "env", "printenv",
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
    tier: PermissionTier
    preview: str


@dataclass(frozen=True)
class ToolReviewRequest:
    tool_call_id: str
    tool_name: str
    preview: str
    permission: PermissionConfig


ToolReviewCallback = Callable[[ToolReviewRequest], Awaitable[ToolReviewDecision]]


def build_permission_hooks(
    settings: Settings,
    *,
    review: ToolReviewCallback | None = None,
) -> Hooks:
    hooks = Hooks()
    skill_roots = [Path(s.path).parent.resolve(strict=False) for s in discover_skills(settings.cwd)]

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

        return ToolExecutionResult(output=PERMISSION_DENIED_OUTPUT, is_error=True)

    return hooks


def permission_decision(permission: PermissionConfig, tier: PermissionTier) -> PermissionDecision:
    if permission.level == "yolo":
        return "allow"
    if tier != "yolo" and _LEVEL_RANK[tier] <= _LEVEL_RANK[permission.level]:
        return "allow"
    return permission.mode


def classify_tool(
    ctx: ToolHookContext[CliDeps],
    *,
    project: str,
    skill_roots: list[Path],
) -> PermissionCheck:
    name = ctx.tool_name.lower()

    if name == "bash":
        command = str(ctx.tool_input.get("command") or "").strip()
        return PermissionCheck(_classify_bash(command), command)

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


def _classify_bash(command: str) -> PermissionTier:
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
    if not words or any(t in _SHELL_CONTROL_TOKENS for t in tokens):
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
