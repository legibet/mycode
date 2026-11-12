"""Context compaction rules. The agent runs the summary request; the functions here are pure."""

from __future__ import annotations

import json

from mycode.messages import (
    CompactTrigger,
    ConversationMessage,
    MessageMeta,
    build_message,
    flatten_message_text,
    text_block,
)
from mycode.usage import Cost, Usage

DEFAULT_COMPACT_THRESHOLD = 0.8


class NothingToCompactError(ValueError):
    """Raised when there is no new context to compact past the latest marker."""


COMPACT_SUMMARY_PROMPT = """\
You are now compacting this conversation to fit the context window. \
Write a handoff summary of the conversation above. \
The summary replaces the conversation, and the next model continues the work from it without asking the user to repeat anything.

Use exactly these sections. Write "(none)" for an empty section.

## Goal
What the user is trying to accomplish, and why. List each goal when the conversation covers more than one.

## Requirements
Constraints, preferences and corrections the user stated. Quote the user's wording where it matters.

## Decisions
Decisions made and approaches rejected, each with its reason.

## Progress
What is done, what is in progress, and what is blocked.

## Relevant Files
Only the files the next model needs to open, each with the reason.

## Next Steps
What remains to do, in order, limited to what the user asked for. Write "(none)" when the latest request is finished.

## Critical Context
Other facts the next model needs, such as error messages, command output and exact values.

Rules:
- Use terse bullets. Keep file paths, identifiers, commands and error strings exact.
- Summarize what the user and the assistant said and did. The system prompt is sent again with every request, so leave it out.
- Leave out what the next model can look up again, such as file contents.
- If the conversation begins with an earlier summary, merge it: keep what is still true, drop what is stale.
- Reply with the summary only, as plain text. Do not continue the work or call tools.\
"""

CONTINUATION_HEADER = "This session is being continued from a previous conversation that was compacted to fit the context window. The summary below covers the earlier part of that conversation. The messages after it are the most recent part, kept verbatim; they are newer than the summary."

TRANSCRIPT_HINT = "For verbatim details not captured in this summary (exact code snippets, error messages, or earlier output), read the original conversation log at: {path}"

CONTINUATION_FOOTER = 'Resume directly from where the work left off. Do not acknowledge this summary, do not recap, and do not preface with "I\'ll continue" or similar.'

COMPACT_ACK = "Acknowledged."

# Token budget of the tail a compaction keeps verbatim after the summary.
TAIL_TOKENS = 20_000

# Estimated tokens of one image or document block.
ATTACHMENT_TOKENS = 1_000


def _latest_marker(messages: list[ConversationMessage]) -> int:
    """Index of the latest compact marker, or -1."""

    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "compact":
            return i
    return -1


def _estimate_tokens(message: ConversationMessage) -> int:
    """Rough token count of one message: 4 characters per token, a flat count per attachment."""

    chars = 0
    attachments = 0
    for block in message["content"]:
        match block:
            case {"type": "text" | "thinking"}:
                chars += len(block["text"])
            case {"type": "tool_use"}:
                chars += len(block["name"]) + len(json.dumps(block["input"], ensure_ascii=False))
            case {"type": "tool_result"}:
                chars += len(block["output"])
                attachments += sum(item["type"] == "image" for item in block.get("content", []))
            case {"type": "image" | "document"}:
                attachments += 1
    return chars // 4 + attachments * ATTACHMENT_TOKENS


def should_compact(messages: list[ConversationMessage], context_window: int, threshold: float) -> bool:
    """True when the context of the next request reaches ``context_window × threshold``.

    The context is the usage the last valid response reported plus an estimate
    of the messages after it. False when no response after the latest marker
    reported usage.
    """

    if threshold <= 0:
        return False
    for i in range(len(messages) - 1, _latest_marker(messages), -1):
        message = messages[i]
        meta = message.get("meta", {})
        if message["role"] != "assistant" or meta.get("stop_reason") in ("error", "cancelled"):
            continue
        if total := (meta.get("usage") or {}).get("total_tokens"):
            tokens = total + sum(_estimate_tokens(later) for later in messages[i + 1 :])
            return tokens >= context_window * threshold
    return False


def _starts_step(message: ConversationMessage) -> bool:
    """A step starts at an assistant message or a user message without tool results, so tool results stay with their call."""

    return message["role"] == "assistant" or all(block["type"] != "tool_result" for block in message["content"])


def _step_to_keep(messages: list[ConversationMessage]) -> int | None:
    """Index where the newest step after the latest marker starts, or ``None`` when there is nothing to compact.

    A compaction keeps that step and summarizes the non-empty messages before
    it that the latest summary does not cover.
    """

    marker = _latest_marker(messages)
    newest = next((i for i in range(len(messages) - 1, marker, -1) if _starts_step(messages[i])), None)
    if newest is None:
        return None
    # The latest summary covers the messages before its tail.
    start = 0 if marker < 0 else messages[marker]["meta"]["keep_from"]
    if any(message["content"] for message in messages[start:newest] if message["role"] != "compact"):
        return newest
    return None


def has_compactable_history(messages: list[ConversationMessage]) -> bool:
    """True when a compaction has something to summarize."""

    return _step_to_keep(messages) is not None


def find_keep_from(messages: list[ConversationMessage], context_window: int) -> int | None:
    """Return the index where the verbatim tail starts, or ``None`` when there is nothing to compact.

    The tail is the newest step, whatever its size, plus the earlier steps
    that fit the token budget. The summary covers the messages before the tail.
    """

    newest = _step_to_keep(messages)
    if newest is None:
        return None
    # A tenth of the window keeps summary plus tail below the threshold on small windows.
    budget = min(TAIL_TOKENS, context_window // 10)
    tokens = sum(_estimate_tokens(message) for message in messages[newest:])
    keep_from = newest
    for i in range(newest - 1, _latest_marker(messages), -1):
        tokens += _estimate_tokens(messages[i])
        if tokens > budget:
            return keep_from
        if _starts_step(messages[i]):
            keep_from = i
    # Every message after the marker fits. Keeping them all would leave nothing new to summarize.
    return newest


def summary_text(response: ConversationMessage) -> str:
    """Return the summary text of a summary response; raise ``ValueError`` when it is unusable."""

    if (stop_reason := response.get("meta", {}).get("stop_reason")) in ("length", "error"):
        raise ValueError(f"compaction summary is incomplete (stop_reason={stop_reason})")
    text = flatten_message_text(response)
    if not text:
        raise ValueError("compaction produced an empty summary")
    return text


def build_marker(
    summary: str,
    *,
    trigger: CompactTrigger,
    provider: str,
    model: str,
    context_window: int,
    keep_from: int,
    usage: Usage | None = None,
    cost: Cost | None = None,
) -> ConversationMessage:
    meta: MessageMeta = {
        "trigger": trigger,
        "provider": provider,
        "model": model,
        "context_window": context_window,
        "keep_from": keep_from,
    }
    if usage:
        meta["usage"] = usage.copy()
    if cost is not None:
        meta["cost"] = cost.copy()
    return build_message("compact", [text_block(summary)], meta=meta)


def project(messages: list[ConversationMessage], transcript_path: str | None) -> list[ConversationMessage]:
    """Return the provider-facing view of ``messages``.

    The latest marker's summary becomes one user message, followed by the tail
    the marker kept (``meta.keep_from`` up to the marker) and the messages
    after the marker. Returns ``messages`` unchanged when there is no marker.
    """

    marker = _latest_marker(messages)
    if marker < 0:
        return messages

    keep_from = messages[marker]["meta"]["keep_from"]
    following = [*messages[keep_from:marker], *messages[marker + 1 :]]
    # The tail holds at least one step. When it starts with a user message,
    # an assistant turn goes before it, so roles alternate.
    continue_now = following[0]["role"] == "assistant"

    parts = [CONTINUATION_HEADER, flatten_message_text(messages[marker])]
    if transcript_path:
        parts.append(TRANSCRIPT_HINT.format(path=transcript_path))
    if continue_now:
        parts.append(CONTINUATION_FOOTER)

    projected = [build_message("user", [text_block("\n\n".join(parts))])]
    if not continue_now:
        projected.append(build_message("assistant", [text_block(COMPACT_ACK)]))
    projected.extend(following)
    return projected
