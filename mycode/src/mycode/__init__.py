"""mycode — multi-turn tool-calling agent runtime."""

from importlib import metadata

from mycode.agent import Agent, Event, PersistCallback, RunResult
from mycode.attachments import Attachment
from mycode.compact import NothingToCompactError
from mycode.hooks import AfterToolHook, BeforeToolHook, HookResult, Hooks, ToolHookContext
from mycode.messages import (
    ContentBlock,
    ConversationMessage,
    assistant_message,
    build_message,
    build_usage,
    document_block,
    flatten_message_text,
    image_block,
    text_block,
    thinking_block,
    tool_result_block,
    tool_use_block,
    user_text_message,
)
from mycode.models import Cost, ModelMetadata, estimate_cost, resolve_model_metadata
from mycode.providers.base import ProviderError, StreamStartTimeoutError
from mycode.session import SessionStore
from mycode.tools import (
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolSpec,
    bash_tool,
    cancel_all_tools,
    edit_tool,
    read_tool,
    tool,
    write_tool,
)

__version__ = metadata.version("mycode-sdk")

__all__ = [
    "Agent",
    "Attachment",
    "ContentBlock",
    "ConversationMessage",
    "Cost",
    "Event",
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
    "assistant_message",
    "bash_tool",
    "build_message",
    "build_usage",
    "cancel_all_tools",
    "document_block",
    "edit_tool",
    "estimate_cost",
    "flatten_message_text",
    "image_block",
    "read_tool",
    "resolve_model_metadata",
    "text_block",
    "thinking_block",
    "tool",
    "tool_result_block",
    "tool_use_block",
    "user_text_message",
    "write_tool",
]
