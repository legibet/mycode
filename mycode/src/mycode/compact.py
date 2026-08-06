"""Context compaction: summarize past history when nearing the context window."""

from __future__ import annotations

from typing import Any

from mycode.messages import ConversationMessage, build_message, text_block

DEFAULT_COMPACT_THRESHOLD = 0.8


class NothingToCompactError(ValueError):
    """Raised when there is no new context to compact past the latest marker."""


COMPACT_SUMMARY_PROMPT = """\
Summarize this conversation to create a continuation document. \
This summary will replace the full conversation history, so it must \
capture everything needed to continue the work seamlessly.

Include:

1. **Task and Intent**: Describe the user's overall goal — what is being \
built, fixed, or investigated, and why.
2. **Decisions and Constraints**: List the decisions made, constraints \
discovered, and approaches chosen or rejected, with the reasoning behind \
each.
3. **User Requests**: Every distinct request or instruction the user gave, \
in chronological order. Preserve the user's original wording for ambiguous \
or nuanced requests.
4. **Files and Changes**: Enumerate every file read, modified, or created \
— paths, what changed, and any code snippets the next turn will need to \
reason about, quoted verbatim.
5. **Errors and Fixes**: List errors encountered with the original message \
verbatim, the cause if known, and the resolution — or that it remains open.
6. **Current State**: What is verified working, what is known broken, what \
is in progress.
7. **Next Step**: The next step to take, with a direct quote from the most \
recent conversation showing where the work left off.

Rules:
- Be specific: reproduce file paths, function names, error messages, and \
other identifiers verbatim — never paraphrase them.
- Do not add suggestions or opinions — only summarize what happened.
- Keep it concise but complete.\
"""

CONTINUATION_HEADER = "This session is being continued from a previous conversation that was compacted to fit the context window. The summary below covers the earlier portion of the conversation."

TRANSCRIPT_HINT = "For verbatim details not captured in this summary (exact code snippets, error messages, or earlier output), read the original conversation log at: {path}"

CONTINUATION_FOOTER = 'Resume directly from where the work left off. Do not acknowledge this summary, do not recap, and do not preface with "I\'ll continue" or similar.'

COMPACT_ACK = "Acknowledged."


def should_compact(
    last_total_tokens: int | None,
    context_window: int | None,
    threshold: float,
) -> bool:
    """True when ``last_total_tokens`` ≥ ``context_window × threshold``.

    The ``(1 - threshold)`` headroom is reserved for the compact LLM call
    itself (see docs/sessions.md).
    """

    if not last_total_tokens or not context_window or threshold <= 0:
        return False
    return last_total_tokens >= context_window * threshold


def has_compactable_history(messages: list[ConversationMessage]) -> bool:
    """True when at least one non-empty user/assistant message follows the latest compact marker."""

    last_compact = -1
    for i, message in enumerate(messages):
        if message.get("role") == "compact":
            last_compact = i

    return any(
        message.get("role") in ("user", "assistant") and message.get("content")
        for message in messages[last_compact + 1 :]
    )


def build_compact_event(
    summary_text: str,
    *,
    provider: str,
    model: str,
    usage: dict[str, Any] | None = None,
) -> ConversationMessage:
    meta: dict[str, Any] = {"provider": provider, "model": model}
    if usage:
        meta["usage"] = dict(usage)
    return build_message("compact", [text_block(summary_text)], meta=meta)


def apply_compact_replay(
    messages: list[ConversationMessage],
    *,
    transcript_path: str | None = None,
) -> list[ConversationMessage]:
    """Replace pre-compact history with a summary continuation.

    Returns ``messages`` unchanged when no compact event is present.
    """

    last_compact = -1
    for i, message in enumerate(messages):
        if message.get("role") == "compact":
            last_compact = i

    if last_compact < 0:
        return messages

    summary_text = ""
    for block in messages[last_compact].get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            summary_text = str(block.get("text") or "")
            break

    tail = [m for m in messages[last_compact + 1 :] if m.get("role") != "compact"]
    # No tail or assistant-led tail = mid-loop; resume directly. A user-led
    # tail needs an "Acknowledged." assistant turn to keep role alternation.
    continue_now = not tail or tail[0].get("role") == "assistant"

    parts = [CONTINUATION_HEADER, summary_text]
    if transcript_path:
        parts.append(TRANSCRIPT_HINT.format(path=transcript_path))
    if continue_now:
        parts.append(CONTINUATION_FOOTER)

    projected = [build_message("user", [text_block("\n\n".join(parts))])]
    if not continue_now:
        projected.append(build_message("assistant", [text_block(COMPACT_ACK)]))
    projected.extend(tail)
    return projected
