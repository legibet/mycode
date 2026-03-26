"""Tests for conversation context compaction."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mycode.core.compact import (
    DEFAULT_COMPACT_THRESHOLD,
    _get_input_tokens,
    apply_compact,
    build_compact_event,
    should_compact,
)
from mycode.core.config import _parse_compact_threshold, get_settings
from mycode.core.messages import build_message, text_block
from mycode.core.session import SessionStore

# ---------------------------------------------------------------------------
# _get_input_tokens — multi-provider usage shapes
# ---------------------------------------------------------------------------


class TestGetInputTokens:
    def test_anthropic_input_tokens(self):
        assert _get_input_tokens({"input_tokens": 5000, "output_tokens": 1000}) == 5000

    def test_openai_chat_prompt_tokens(self):
        assert _get_input_tokens({"prompt_tokens": 7000, "completion_tokens": 500}) == 7000

    def test_gemini_prompt_token_count(self):
        assert _get_input_tokens({"prompt_token_count": 3000, "candidates_token_count": 800}) == 3000

    def test_empty_usage(self):
        assert _get_input_tokens({}) == 0

    def test_priority_input_tokens_over_prompt_tokens(self):
        assert _get_input_tokens({"input_tokens": 100, "prompt_tokens": 200}) == 100


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    def test_triggers_above_threshold(self):
        assert should_compact({"input_tokens": 85000}, 100000, 0.8)

    def test_triggers_at_exact_threshold(self):
        assert should_compact({"input_tokens": 80000}, 100000, 0.8)

    def test_no_trigger_below_threshold(self):
        assert not should_compact({"input_tokens": 79999}, 100000, 0.8)

    def test_no_trigger_without_usage(self):
        assert not should_compact(None, 100000, 0.8)

    def test_no_trigger_without_context_window(self):
        assert not should_compact({"input_tokens": 50000}, None, 0.8)

    def test_no_trigger_when_disabled(self):
        assert not should_compact({"input_tokens": 99999}, 100000, 0.0)

    def test_no_trigger_negative_threshold(self):
        assert not should_compact({"input_tokens": 99999}, 100000, -1.0)

    def test_works_with_openai_chat_usage(self):
        assert should_compact({"prompt_tokens": 85000}, 100000, 0.8)

    def test_works_with_gemini_usage(self):
        assert should_compact({"prompt_token_count": 85000}, 100000, 0.8)


# ---------------------------------------------------------------------------
# build_compact_event
# ---------------------------------------------------------------------------


class TestBuildCompactEvent:
    def test_basic_structure(self):
        event = build_compact_event(
            "summary text",
            provider="anthropic",
            model="claude-sonnet-4-6",
            compacted_count=10,
        )
        assert event["role"] == "compact"
        assert event["content"] == [{"type": "text", "text": "summary text"}]
        assert event["meta"]["provider"] == "anthropic"
        assert event["meta"]["model"] == "claude-sonnet-4-6"
        assert event["meta"]["compacted_count"] == 10
        assert "usage" not in event["meta"]

    def test_with_usage(self):
        usage = {"input_tokens": 5000, "output_tokens": 1000}
        event = build_compact_event(
            "summary",
            provider="anthropic",
            model="test",
            compacted_count=5,
            usage=usage,
        )
        assert event["meta"]["usage"] == usage

    def test_serializable(self):
        event = build_compact_event("s", provider="p", model="m", compacted_count=1)
        json.dumps(event)  # should not raise


# ---------------------------------------------------------------------------
# apply_compact
# ---------------------------------------------------------------------------


class TestApplyCompact:
    def test_no_compact_returns_unchanged(self):
        msgs = [
            build_message("user", [text_block("hello")]),
            build_message("assistant", [text_block("hi")]),
        ]
        result = apply_compact(msgs)
        assert result is msgs

    def test_basic_compaction(self):
        msgs = [
            build_message("user", [text_block("msg1")]),
            build_message("assistant", [text_block("reply1")]),
            build_compact_event("the summary", provider="p", model="m", compacted_count=2),
            build_message("user", [text_block("msg2")]),
            build_message("assistant", [text_block("reply2")]),
        ]
        result = apply_compact(msgs)

        assert len(result) == 4  # summary_user + summary_ack + msg2 + reply2
        assert result[0]["role"] == "user"
        assert "the summary" in result[0]["content"][0]["text"]
        assert result[1]["role"] == "assistant"
        assert result[2] == msgs[3]  # msg2 preserved
        assert result[3] == msgs[4]  # reply2 preserved

    def test_multiple_compacts_uses_last(self):
        msgs = [
            build_message("user", [text_block("msg1")]),
            build_compact_event("old summary", provider="p", model="m", compacted_count=1),
            build_message("user", [text_block("msg2")]),
            build_compact_event("new summary", provider="p", model="m", compacted_count=3),
            build_message("user", [text_block("msg3")]),
        ]
        result = apply_compact(msgs)

        assert len(result) == 3  # summary_user + summary_ack + msg3
        assert "new summary" in result[0]["content"][0]["text"]

    def test_compact_at_end_no_trailing_messages(self):
        msgs = [
            build_message("user", [text_block("msg1")]),
            build_message("assistant", [text_block("reply1")]),
            build_compact_event("summary", provider="p", model="m", compacted_count=2),
        ]
        result = apply_compact(msgs)
        assert len(result) == 2  # summary_user + summary_ack only
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"

    def test_alternating_roles_after_compact(self):
        """After compaction the message list must alternate user/assistant."""
        msgs = [
            build_message("user", [text_block("m1")]),
            build_compact_event("summary", provider="p", model="m", compacted_count=1),
            build_message("user", [text_block("m2")]),
        ]
        result = apply_compact(msgs)
        roles = [m["role"] for m in result]
        assert roles == ["user", "assistant", "user"]

    def test_empty_list(self):
        assert apply_compact([]) == []


# ---------------------------------------------------------------------------
# _parse_compact_threshold (config)
# ---------------------------------------------------------------------------


class TestParseCompactThreshold:
    def test_none_returns_none(self):
        assert _parse_compact_threshold(None) is None

    def test_false_returns_zero(self):
        assert _parse_compact_threshold(False) == 0.0

    def test_zero_returns_zero(self):
        assert _parse_compact_threshold(0) == 0.0

    def test_valid_float(self):
        assert _parse_compact_threshold(0.8) == 0.8

    def test_valid_upper_bound(self):
        assert _parse_compact_threshold(1.0) == 1.0

    def test_over_one_returns_none(self):
        assert _parse_compact_threshold(1.5) is None

    def test_negative_returns_none(self):
        assert _parse_compact_threshold(-0.1) is None

    def test_string_number(self):
        assert _parse_compact_threshold("0.9") == 0.9

    def test_invalid_string_returns_none(self):
        assert _parse_compact_threshold("abc") is None


class TestCompactThresholdConfig:
    """Config loading integrates compact_threshold correctly."""

    @pytest.fixture(autouse=True)
    def _disable_live_models_dev_lookup(self, monkeypatch) -> None:
        monkeypatch.setattr("mycode.core.config.lookup_model_metadata", lambda **_: None)

    @pytest.fixture(autouse=True)
    def _clear_provider_env(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_default_is_none(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / ".mycode"
        monkeypatch.setenv("MYCODE_HOME", str(home))
        settings = get_settings(str(tmp_path))
        assert settings.compact_threshold is None

    def test_reads_from_config(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / ".mycode"
        monkeypatch.setenv("MYCODE_HOME", str(home))
        self._write(
            home / "config.json",
            json.dumps({"default": {"compact_threshold": 0.7}}),
        )
        settings = get_settings(str(tmp_path))
        assert settings.compact_threshold == 0.7

    def test_false_disables(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / ".mycode"
        monkeypatch.setenv("MYCODE_HOME", str(home))
        self._write(
            home / "config.json",
            json.dumps({"default": {"compact_threshold": False}}),
        )
        settings = get_settings(str(tmp_path))
        assert settings.compact_threshold == 0.0

    def test_workspace_overrides_global(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home" / ".mycode"
        project = tmp_path / "project"
        project.mkdir(parents=True)
        (project / ".git").mkdir()
        monkeypatch.setenv("MYCODE_HOME", str(home))
        self._write(home / "config.json", json.dumps({"default": {"compact_threshold": 0.7}}))
        self._write(project / ".mycode" / "config.json", json.dumps({"default": {"compact_threshold": 0.9}}))
        settings = get_settings(str(project))
        assert settings.compact_threshold == 0.9


# ---------------------------------------------------------------------------
# Session loading with compact events
# ---------------------------------------------------------------------------


class TestSessionCompact:
    @pytest.fixture
    def temp_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield SessionStore(data_dir=Path(tmpdir))

    @pytest.mark.asyncio
    async def test_load_session_applies_compact(self, temp_store):
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        for msg in [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            build_compact_event("the summary", provider="p", model="m", compacted_count=2),
            {"role": "user", "content": [{"type": "text", "text": "next question"}]},
        ]:
            await temp_store.append_message(sid, msg, provider="p", model="m", cwd="/tmp", api_base=None)

        loaded = await temp_store.load_session(sid)
        msgs = loaded["messages"]

        # Should have: summary_user + summary_ack + next question
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert "the summary" in msgs[0]["content"][0]["text"]
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["content"][0]["text"] == "next question"

    @pytest.mark.asyncio
    async def test_load_session_no_compact_unchanged(self, temp_store):
        result = await temp_store.create_session(title="Test", model="m", cwd="/tmp", api_base=None)
        sid = result["session"]["id"]

        await temp_store.append_message(
            sid,
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            provider="p",
            model="m",
            cwd="/tmp",
            api_base=None,
        )
        await temp_store.append_message(
            sid,
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            provider="p",
            model="m",
            cwd="/tmp",
            api_base=None,
        )

        loaded = await temp_store.load_session(sid)
        msgs = loaded["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_original_messages_preserved_in_jsonl(self, temp_store):
        """Compact is non-destructive — original messages remain in the JSONL file."""
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
        assert json.loads(raw_lines[0])["role"] == "user"
        assert json.loads(raw_lines[1])["role"] == "assistant"
        assert json.loads(raw_lines[2])["role"] == "compact"


# ---------------------------------------------------------------------------
# Agent compact_threshold defaults
# ---------------------------------------------------------------------------


class TestAgentCompactThreshold:
    def test_default_threshold(self):
        from mycode.core.agent import Agent

        agent = Agent(model="m", cwd="/tmp", session_dir=Path("/tmp/s"))
        assert agent.compact_threshold == DEFAULT_COMPACT_THRESHOLD

    def test_explicit_threshold(self):
        from mycode.core.agent import Agent

        agent = Agent(model="m", cwd="/tmp", session_dir=Path("/tmp/s"), compact_threshold=0.9)
        assert agent.compact_threshold == 0.9

    def test_zero_disables(self):
        from mycode.core.agent import Agent

        agent = Agent(model="m", cwd="/tmp", session_dir=Path("/tmp/s"), compact_threshold=0.0)
        assert agent.compact_threshold == 0.0
        # Verify it won't trigger
        assert not should_compact({"input_tokens": 99999}, 100000, agent.compact_threshold)

    def test_none_uses_default(self):
        from mycode.core.agent import Agent

        agent = Agent(model="m", cwd="/tmp", session_dir=Path("/tmp/s"), compact_threshold=None)
        assert agent.compact_threshold == DEFAULT_COMPACT_THRESHOLD
