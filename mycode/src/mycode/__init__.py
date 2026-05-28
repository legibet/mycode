"""mycode — multi-turn tool-calling agent runtime.

Public API for embedding the agent loop in other Python applications. The
runtime ships four built-in coding tools (``read``, ``write``, ``edit``,
``bash``) exposed as :data:`read_tool`, :data:`write_tool`, :data:`edit_tool`,
:data:`bash_tool` — pick the ones you want via ``tools=[...]`` rather than
silently exposing file system and shell access.
"""

from importlib import metadata

from mycode.agent import Agent, Event, PersistCallback, RunResult
from mycode.attachments import Attachment
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
from mycode.session import SessionStore
from mycode.tools import (
    DEFAULT_TOOL_SPECS,
    ToolContext,
    ToolExecutionResult,
    ToolExecutor,
    ToolSpec,
    cancel_all_tools,
    tool,
)

# The package metadata in mycode/pyproject.toml is the single version source.
__version__ = metadata.version("mycode-sdk")

read_tool, write_tool, edit_tool, bash_tool = DEFAULT_TOOL_SPECS

__all__ = [
    "Agent",
    "Attachment",
    "ContentBlock",
    "ConversationMessage",
    "DEFAULT_TOOL_SPECS",
    "Event",
    "AfterToolHook",
    "BeforeToolHook",
    "HookResult",
    "Hooks",
    "PersistCallback",
    "RunResult",
    "SessionStore",
    "ToolContext",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolHookContext",
    "ToolSpec",
    "__version__",
    "assistant_message",
    "bash_tool",
    "build_message",
    "cancel_all_tools",
    "document_block",
    "edit_tool",
    "flatten_message_text",
    "image_block",
    "read_tool",
    "text_block",
    "thinking_block",
    "tool",
    "tool_result_block",
    "tool_use_block",
    "user_text_message",
    "write_tool",
]
