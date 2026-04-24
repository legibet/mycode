"""Tool execution hooks for the agent runtime."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from mycode.tools import ToolExecutionResult, ToolSpec

type HookResult = ToolExecutionResult | None
type BeforeToolHook = Callable[["ToolHookContext"], HookResult | Awaitable[HookResult]]
type AfterToolHook = Callable[["ToolHookContext", ToolExecutionResult], HookResult | Awaitable[HookResult]]


@dataclass(frozen=True)
class ToolHookContext:
    """Read-only context passed to tool execution hooks."""

    session_id: str
    cwd: str
    provider: str
    model: str
    tool_call_id: str
    tool_name: str
    tool_input: Mapping[str, Any]
    tool: ToolSpec

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_input", _freeze(self.tool_input))


class Hooks:
    """Ordered callbacks around model-requested tool execution."""

    def __init__(
        self,
        *,
        before_tool: Sequence[BeforeToolHook] = (),
        after_tool: Sequence[AfterToolHook] = (),
    ) -> None:
        self._before_tool = list(before_tool)
        self._after_tool = list(after_tool)

    def before_tool(self, hook: BeforeToolHook) -> BeforeToolHook:
        """Register a hook that can return a tool result before execution."""

        self._before_tool.append(hook)
        return hook

    def after_tool(self, hook: AfterToolHook) -> AfterToolHook:
        """Register a hook that can observe or replace a tool result."""

        self._after_tool.append(hook)
        return hook

    async def run_before_tool(self, ctx: ToolHookContext) -> ToolExecutionResult | None:
        for hook in self._before_tool:
            result = await _resolve(hook(ctx))
            if result is not None:
                return result
        return None

    async def run_after_tool(self, ctx: ToolHookContext, result: ToolExecutionResult) -> ToolExecutionResult:
        for hook in self._after_tool:
            replacement = await _resolve(hook(ctx, result))
            if replacement is not None:
                result = replacement
        return result


async def _resolve(value: object) -> HookResult:
    if inspect.isawaitable(value):
        value = await value
    if value is None or isinstance(value, ToolExecutionResult):
        return value
    raise TypeError(f"tool hook returned unsupported value: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    """Recursively wrap mappings in MappingProxyType and lists/tuples in tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "AfterToolHook",
    "BeforeToolHook",
    "HookResult",
    "Hooks",
    "ToolHookContext",
]
