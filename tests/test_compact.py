"""Focused tests for conversation context compaction."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mycode.core.compact import DEFAULT_COMPACT_THRESHOLD, _get_input_tokens, build_compact_event, should_compact
from mycode.core.config import get_settings
from mycode.core.session import SessionStore


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_workspace_config_overrides_global_compact_threshold(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home" / ".mycode"
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    monkeypatch.setenv("MYCODE_HOME", str(home))

    _write(home / "config.json", json.dumps({"default": {"compact_threshold": 0.7}}))
    _write(project / ".mycode" / "config.json", json.dumps({"default": {"compact_threshold": 0.9}}))

    settings = get_settings(str(project))

    assert settings.compact_threshold == 0.9


@pytest.mark.parametrize(
    ("usage", "expected_tokens"),
    [
        ({"input_tokens": 5000}, 5000),
        ({"prompt_tokens": 7000}, 7000),
        ({"prompt_token_count": 3000}, 3000),
    ],
)
def test_get_input_tokens_supports_provider_specific_usage_shapes(usage: dict[str, int], expected_tokens: int) -> None:
    assert _get_input_tokens(usage) == expected_tokens


def test_should_compact_respects_threshold_boundaries() -> None:
    assert should_compact({"input_tokens": 80000}, 100000, 0.8) is True
    assert should_compact({"input_tokens": 79999}, 100000, 0.8) is False
    assert should_compact({"input_tokens": 99999}, 100000, 0.0) is False


class TestSessionCompact:
    @pytest.fixture
    def temp_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield SessionStore(data_dir=Path(tmpdir))

    @pytest.mark.asyncio
    async def test_load_session_applies_latest_compact_summary(self, temp_store):
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            build_compact_event("old summary", provider="p", model="m", compacted_count=2),
            {"role": "user", "content": [{"type": "text", "text": "next"}]},
            build_compact_event("new summary", provider="p", model="m", compacted_count=4),
            {"role": "assistant", "content": [{"type": "text", "text": "latest reply"}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        loaded = await temp_store.load_session(sid)

        assert loaded["messages"] == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "[Conversation Summary]\n\nnew summary"}],
                "meta": {"synthetic": True},
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Understood. I have the context from the conversation summary and will continue the work.",
                    }
                ],
                "meta": {"synthetic": True},
            },
            {"role": "assistant", "content": [{"type": "text", "text": "latest reply"}]},
        ]

    @pytest.mark.asyncio
    async def test_original_messages_preserved_in_jsonl(self, temp_store):
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            build_compact_event("summary", provider="p", model="m", compacted_count=2),
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        raw_lines = temp_store.messages_path(sid).read_text().strip().splitlines()

        assert len(raw_lines) == 3
        assert json.loads(raw_lines[2])["role"] == "compact"


def test_apply_compact_marks_synthetic_messages():
    """Compact-synthesized summary and ack should carry meta.synthetic = True."""
    from mycode.core.compact import apply_compact

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        build_compact_event("summary", provider="p", model="m", compacted_count=2),
    ]
    result = apply_compact(messages)

    assert result[0]["meta"]["synthetic"] is True
    assert result[1]["meta"]["synthetic"] is True


def test_agent_uses_default_compact_threshold():
    from mycode.core.agent import Agent

    agent = Agent(model="m", cwd="/tmp", session_dir=Path("/tmp/s"))

    assert agent.compact_threshold == DEFAULT_COMPACT_THRESHOLD
