"""Conversation context compaction.

When a conversation approaches the model's context window limit, the agent
generates a structured summary and appends a ``compact`` event to the session
JSONL.  All original messages are preserved in the file — compaction is a
non-destructive, append-only event.
"""

from __future__ import annotations

from typing import Any

from mycode.core.messages import ConversationMessage, build_message, text_block

DEFAULT_COMPACT_THRESHOLD = 0.8

COMPACT_SUMMARY_PROMPT = """\
Summarize this conversation to create a continuation document. \
This summary will replace the full conversation history, so it must \
capture everything needed to continue the work seamlessly.

Include:

1. **User Requests**: Every distinct request or instruction the user gave, \
in chronological order. Preserve the user's original wording for ambiguous \
or nuanced requests.
2. **Completed Work**: What was accomplished — files created, modified, or \
deleted; bugs fixed; features added. Include file paths and function names.
3. **Current State**: The exact state of the work right now — what is working, \
what is broken, what is partially done.
4. **Key Decisions**: Important decisions made, constraints discovered, \
approaches chosen or rejected, and why.
5. **Next Steps**: What remains to be done, any work that was in progress \
when this summary was generated.

Rules:
- Be specific: include file paths, function names, error messages, and \
concrete details.
- Do not add suggestions or opinions — only summarize what happened.
- Keep it concise but complete.\
"""

_COMPACT_ACK = "Understood. I have the context from the conversation summary and will continue the work."


def should_compact(
    last_usage: dict[str, Any] | None,
    context_window: int | None,
    threshold: float,
) -> bool:
    """Return True when the last response's input tokens exceed the threshold."""
    if not last_usage or not context_window or threshold <= 0:
        return False
    input_tokens = last_usage.get("input_tokens", 0)
    return input_tokens >= context_window * threshold


def build_compact_event(
    summary_text: str,
    *,
    provider: str,
    model: str,
    compacted_count: int,
    usage: dict[str, Any] | None = None,
) -> ConversationMessage:
    """Build the compact event dict to append to session JSONL."""
    meta: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "compacted_count": compacted_count,
    }
    if usage is not None:
        meta["usage"] = usage
    return build_message("compact", [text_block(summary_text)], meta=meta)


def apply_compact(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Transform a message list containing compact events for provider consumption.

    Finds the last ``role: "compact"`` event, converts it to a user message
    plus a synthetic assistant acknowledgement, and returns those followed by
    any messages recorded after the compact event.

    If no compact event exists the original list is returned unchanged.
    """
    last_compact_idx: int | None = None
    for i, msg in enumerate(messages):
        if msg.get("role") == "compact":
            last_compact_idx = i

    if last_compact_idx is None:
        return messages

    summary_text = _extract_summary_text(messages[last_compact_idx])
    summary_user = build_message(
        "user",
        [text_block(f"[Conversation Summary]\n\n{summary_text}")],
    )
    summary_ack = build_message("assistant", [text_block(_COMPACT_ACK)])

    return [summary_user, summary_ack] + messages[last_compact_idx + 1 :]


def _extract_summary_text(compact_event: ConversationMessage) -> str:
    """Extract the summary text from a compact event's content blocks."""
    for block in compact_event.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""
