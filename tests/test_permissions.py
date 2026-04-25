"""Tests for CLI permission classification and hooks."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycode import ToolHookContext
from mycode.tools import ToolExecutionResult, ToolSpec
from mycode_cli.config import PermissionConfig, Settings
from mycode_cli.permissions import (
    PERMISSION_DENIED_BY_USER_OUTPUT,
    PERMISSION_DENIED_OUTPUT,
    ToolReviewDecision,
    ToolReviewRequest,
    build_permission_hooks,
    classify_tool,
    permission_decision,
)

_SPEC = ToolSpec(
    name="test",
    description="Test tool.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": True},
    runner=lambda _ctx, _args: ToolExecutionResult(output="ok"),
)


def _ctx(name: str, tool_input: dict[str, object]) -> ToolHookContext:
    return ToolHookContext(
        session_id="s",
        cwd="/tmp",
        provider="openai",
        model="gpt-5.4",
        tool_call_id="call-1",
        tool_name=name,
        tool_input=tool_input,
        tool=_SPEC,
    )


def _settings(tmp_path: Path, *, permission: PermissionConfig | None = None) -> Settings:
    return Settings(
        providers={},
        default_provider=None,
        default_model=None,
        port=8000,
        cwd=str(tmp_path),
        permission=permission or PermissionConfig(),
        config_paths=[],
    )


def test_permission_decision_uses_level_then_mode() -> None:
    assert permission_decision(PermissionConfig(level="safe", mode="ask"), "readonly") == "allow"
    assert permission_decision(PermissionConfig(level="safe", mode="ask"), "safe") == "allow"
    assert permission_decision(PermissionConfig(level="safe", mode="ask"), "standard") == "ask"
    assert permission_decision(PermissionConfig(level="safe", mode="deny"), "standard") == "deny"
    assert permission_decision(PermissionConfig(level="yolo", mode="deny"), "yolo") == "allow"


def test_classifies_structured_tools_by_cwd_and_skill_paths(tmp_path: Path) -> None:
    skill_dir = tmp_path.parent / "skills" / "demo"
    skill_dir.mkdir(parents=True)

    assert (
        classify_tool(
            _ctx("read", {"path": "src/app.py"}),
            cwd=str(tmp_path),
            skill_roots=[skill_dir],
        ).tier
        == "readonly"
    )
    assert (
        classify_tool(
            _ctx("write", {"path": "src/app.py"}),
            cwd=str(tmp_path),
            skill_roots=[skill_dir],
        ).tier
        == "safe"
    )
    assert (
        classify_tool(
            _ctx("read", {"path": str(skill_dir / "SKILL.md")}),
            cwd=str(tmp_path),
            skill_roots=[skill_dir],
        ).tier
        == "readonly"
    )
    assert (
        classify_tool(
            _ctx("edit", {"path": str(tmp_path.parent / "outside.py")}),
            cwd=str(tmp_path),
            skill_roots=[skill_dir],
        ).tier
        == "yolo"
    )


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status --short",
        "git diff -- src/app.py",
        "rg TODO src",
        "find src -name '*.py'",
        "command -v pytest",
    ],
)
def test_classifies_common_readonly_bash(command: str, tmp_path: Path) -> None:
    check = classify_tool(_ctx("bash", {"command": command}), cwd=str(tmp_path), skill_roots=[])
    assert check.tier == "readonly"


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests",
        "uv run ruff check",
        "pnpm --dir web test:run",
        "npm run build",
        "python -m pytest",
        "go test ./...",
        "cargo check",
        "just check",
        "uv sync --dev",
        "pnpm install",
        # awk/sed are scripting languages and must not be auto-readonly.
        "awk '{print $1}' file.txt",
        "sed 's/foo/bar/' file.txt",
    ],
)
def test_classifies_single_non_dangerous_bash_as_standard(command: str, tmp_path: Path) -> None:
    check = classify_tool(_ctx("bash", {"command": command}), cwd=str(tmp_path), skill_roots=[])
    assert check.tier == "standard"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf dist",
        "git reset --hard HEAD",
        "git push --force",
        "ls\npwd",
        "sleep 1 &",
        "ls && rm -rf dist",
        "grep foo a.txt | wc -l",
        "echo hi > out.txt",
        "echo hi 2>&1",
        "sed -i 's/a/b/' file.txt",
        "find . -name x -delete",
        "find . -name x -exec rm {} ;",
        "find . -name x -execdir touch out ;",
        "find . -name x -ok rm {} ;",
        "find . -name x -okdir rm {} ;",
        "find . -fprint /tmp/out",
        "find . -fprint0 /tmp/out",
        "find . -fprintf /tmp/out %p\\n",
        "find . -fls /tmp/out",
    ],
)
def test_classifies_dangerous_or_compound_bash_as_yolo(command: str, tmp_path: Path) -> None:
    check = classify_tool(_ctx("bash", {"command": command}), cwd=str(tmp_path), skill_roots=[])
    assert check.tier == "yolo"


@pytest.mark.asyncio
async def test_permission_hook_denies_without_interactive_review_without_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mycode_cli.permissions.discover_skills", lambda _cwd: [])
    cancelled = False

    def cancel() -> None:
        nonlocal cancelled
        cancelled = True

    hooks = build_permission_hooks(
        _settings(tmp_path, permission=PermissionConfig(level="safe", mode="ask")), on_user_denied=cancel
    )
    result = await hooks.run_before_tool(_ctx("bash", {"command": "pnpm install"}))

    assert cancelled is False
    assert result == ToolExecutionResult(output=PERMISSION_DENIED_OUTPUT, is_error=True)


@pytest.mark.asyncio
async def test_permission_hook_allows_interactive_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mycode_cli.permissions.discover_skills", lambda _cwd: [])
    reviewed: list[ToolReviewRequest] = []

    async def review(request: ToolReviewRequest) -> ToolReviewDecision:
        reviewed.append(request)
        return "allow"

    hooks = build_permission_hooks(
        _settings(tmp_path, permission=PermissionConfig(level="safe", mode="ask")),
        review=review,
    )
    result = await hooks.run_before_tool(_ctx("bash", {"command": "pnpm install"}))

    assert result is None
    assert reviewed[0].preview == "pnpm install"


@pytest.mark.asyncio
async def test_permission_hook_distinguishes_user_denial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mycode_cli.permissions.discover_skills", lambda _cwd: [])
    cancelled = False

    def cancel() -> None:
        nonlocal cancelled
        cancelled = True

    async def review(_request: ToolReviewRequest) -> ToolReviewDecision:
        return "deny"

    hooks = build_permission_hooks(
        _settings(tmp_path, permission=PermissionConfig(level="safe", mode="ask")),
        review=review,
        on_user_denied=cancel,
    )
    result = await hooks.run_before_tool(_ctx("bash", {"command": "pnpm install"}))

    assert cancelled is True
    assert result == ToolExecutionResult(output=PERMISSION_DENIED_BY_USER_OUTPUT, is_error=True)
