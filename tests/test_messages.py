"""Message helper behaviour."""

from __future__ import annotations

from mycode import build_message, flatten_message_text, text_block


def test_flatten_skips_payload_blocks() -> None:
    job = {"job": {"tool_use_id": "toolu_1", "name": "bash", "label": "pytest -q", "exit_code": 0}}
    message = build_message(
        "user",
        [
            text_block("Background bash finished", meta=job),
            text_block("skill", meta={"skill_snapshot": True}),
            text_block("<file>…</file>", meta={"attachment": True}),
            text_block("keep this"),
        ],
    )
    assert flatten_message_text(message) == "keep this"
