"""mycode — multi-turn tool-calling agent runtime."""

from importlib import metadata

from mycode.agent import Agent, PersistCallback, RunResult
from mycode.attachments import Attachment
from mycode.compact import NothingToCompactError
from mycode.events import (
    CancelledEvent,
    CompactEvent,
    ErrorEvent,
    Event,
    ReasoningDoneEvent,
    ReasoningEvent,
    RetryEvent,
    TextEvent,
    ToolDoneEvent,
    ToolOutputEvent,
    ToolStartEvent,
    UsageEvent,
    UserMessageEvent,
)
from mycode.hooks import AfterToolHook, BeforeToolHook, HookResult, Hooks, ToolHookContext
from mycode.messages import (
    ContentBlock,
    ConversationMessage,
    assistant_message,
    build_message,
    document_block,
    flatten_message_text,
    image_block,
    text_block,
    thinking_block,
    tool_result_block,
    tool_use_block,
    user_text_message,
)
from mycode.models import ModelMetadata, resolve_model_metadata
from mycode.providers.base import ProviderError, StreamStartTimeoutError
from mycode.session import SessionStore
from mycode.tools import (
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolSpec,
    tool,
)
from mycode.usage import Cost, add_cost, add_usage, build_usage, estimate_cost

__version__ = metadata.version("mycode-sdk")

__all__ = [
    "CancelledEvent",
    "CompactEvent",
    "ErrorEvent",
    "Event",
    "ReasoningDoneEvent",
    "ReasoningEvent",
    "RetryEvent",
    "TextEvent",
    "ToolDoneEvent",
    "ToolOutputEvent",
    "ToolStartEvent",
    "UsageEvent",
    "UserMessageEvent",
    "Agent",
    "Attachment",
    "ContentBlock",
    "ConversationMessage",
    "Cost",
    "AfterToolHook",
    "BeforeToolHook",
    "HookResult",
    "Hooks",
    "ModelMetadata",
    "NothingToCompactError",
    "PersistCallback",
    "ProviderError",
    "RunResult",
    "SessionStore",
    "StreamStartTimeoutError",
    "ToolContext",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolHookContext",
    "ToolSpec",
    "__version__",
    "add_cost",
    "add_usage",
    "assistant_message",
    "build_message",
    "build_usage",
    "document_block",
    "estimate_cost",
    "flatten_message_text",
    "image_block",
    "resolve_model_metadata",
    "text_block",
    "thinking_block",
    "tool",
    "tool_result_block",
    "tool_use_block",
    "user_text_message",
]
