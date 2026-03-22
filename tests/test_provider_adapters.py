from __future__ import annotations

from typing import Any, cast

from mycode.core.providers import (
    AnthropicAdapter,
    DeepSeekAdapter,
    MiniMaxAdapter,
    MoonshotAIAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    OpenRouterAdapter,
    ZAIAdapter,
)
from mycode.core.tools import DEFAULT_TOOL_SPECS


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


def test_openai_responses_builds_initial_input_items() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ],
        ),
    )

    prepared_messages = adapter.prepare_messages(request)
    input_items, previous_response_id = adapter._build_input_items(request, prepared_messages)

    assert previous_response_id is None
    assert input_items == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


def test_openai_responses_uses_previous_response_id_for_tool_results() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                    "meta": {"provider": "openai", "model": "gpt-5.4", "provider_message_id": "resp_123"},
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"}],
                },
            ],
        ),
    )

    prepared_messages = adapter.prepare_messages(request)
    input_items, previous_response_id = adapter._build_input_items(request, prepared_messages)

    assert previous_response_id == "resp_123"
    assert input_items == [{"type": "function_call_output", "call_id": "call_1", "output": "file contents"}]


def test_openai_responses_falls_back_to_full_replay_for_cross_provider_history() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "double 21"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need the tool first."},
                        {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
                    ],
                    "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "42"}],
                },
            ],
        ),
    )

    prepared_messages = adapter.prepare_messages(request)
    input_items, previous_response_id = adapter._build_input_items(request, prepared_messages)

    assert previous_response_id is None
    assert input_items == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "double 21"}],
        },
        {
            "type": "message",
            "id": "replay_assistant_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Need the tool first."}],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read",
            "arguments": '{"path": "x.py"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "42",
        },
    ]


def test_openai_responses_ignores_previous_response_id_when_later_assistant_exists() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "openai turn"}],
                    "meta": {"provider": "openai", "model": "gpt-5.4", "provider_message_id": "resp_123"},
                },
                {"role": "user", "content": [{"type": "text", "text": "switched"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "other provider"}],
                    "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                },
                {"role": "user", "content": [{"type": "text", "text": "come back"}]},
            ],
        ),
    )

    prepared_messages = adapter.prepare_messages(request)
    _, previous_response_id = adapter._build_input_items(request, prepared_messages)

    assert previous_response_id is None


def test_openai_responses_build_request_payload_includes_prompt_cache_key() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id="session_123",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ],
            system="You are helpful.",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    payload = adapter._build_request_payload(request)

    assert payload["prompt_cache_key"] == "session_123"


def test_openai_responses_converts_final_response_blocks() -> None:
    adapter = OpenAIResponsesAdapter()
    response = _Obj(
        id="resp_123",
        model="gpt-5.4",
        status="completed",
        usage=_Obj(input_tokens=10, output_tokens=5),
        output=[
            _Obj(type="reasoning", id="rs_1", status="completed", content=[_Obj(text="think")], summary=[]),
            _Obj(type="message", content=[_Obj(type="output_text", text="answer", annotations=[])]),
            _Obj(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="read",
                arguments='{"path": "x.py"}',
                status="completed",
            ),
        ],
    )

    message = adapter._convert_final_response(response)

    assert message["role"] == "assistant"
    assert message["content"][0]["type"] == "thinking"
    assert message["content"][0]["text"] == "think"
    assert message["content"][0]["meta"] == {"native": {"item_id": "rs_1", "status": "completed"}}
    assert message["content"][1] == {"type": "text", "text": "answer"}
    assert message["content"][2]["type"] == "tool_use"
    assert message["content"][2]["id"] == "call_1"
    assert message["content"][2]["input"] == {"path": "x.py"}
    assert message["content"][2]["meta"] == {"native": {"item_id": "fc_1", "status": "completed"}}


def test_openai_responses_serializes_strict_tool_schemas() -> None:
    adapter = OpenAIResponsesAdapter()

    serialized_tools = [
        adapter._serialize_tool(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
        )
        for tool in DEFAULT_TOOL_SPECS
    ]

    for tool in serialized_tools:
        parameters = tool["parameters"]
        assert tool["strict"] is True
        assert parameters["required"] == list(parameters["properties"].keys())

    read_tool = next(tool for tool in serialized_tools if tool["name"] == "read")
    assert read_tool["parameters"]["properties"]["offset"]["type"] == ["integer", "null"]
    assert read_tool["parameters"]["properties"]["limit"]["type"] == ["integer", "null"]

    bash_tool = next(tool for tool in serialized_tools if tool["name"] == "bash")
    assert bash_tool["parameters"]["properties"]["timeout"]["type"] == ["integer", "null"]

    read_schema = next(tool for tool in DEFAULT_TOOL_SPECS if tool.name == "read").input_schema
    assert read_schema["required"] == ["path"]


def test_openai_chat_extracts_reasoning_from_known_extra_fields() -> None:
    adapter = OpenAIChatAdapter()

    delta = _Obj(reasoning_content="step zero")
    text, meta = adapter._extract_reasoning_delta(delta)
    assert text == "step zero"
    assert meta == {"reasoning_field": "reasoning_content"}

    delta = _Obj(model_extra={"reasoning_content": "step one"})
    text, meta = adapter._extract_reasoning_delta(delta)
    assert text == "step one"
    assert meta == {"reasoning_field": "reasoning_content"}

    delta = _Obj(
        model_extra={
            "reasoning_details": [
                {"type": "reasoning.text", "text": "step "},
                {"type": "reasoning.text", "text": "two"},
            ]
        }
    )
    text, meta = adapter._extract_reasoning_delta(delta)
    assert text == "step two"
    assert meta["reasoning_field"] == "reasoning_details"


def test_provider_prepare_messages_closes_interrupted_tool_loop() -> None:
    adapter = OpenAIChatAdapter()
    original_messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
            "meta": {"provider": "openai_chat", "model": "test-model"},
        },
        {"role": "user", "content": [{"type": "text", "text": "next question"}]},
    ]
    request = cast(Any, _Obj(model="test-model", messages=original_messages))

    prepared_messages = adapter.prepare_messages(request)

    assert original_messages == request.messages
    assert prepared_messages == [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
            "meta": {"provider": "openai_chat", "model": "test-model"},
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "error: tool call was interrupted (no result recorded)",
                    "is_error": True,
                }
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "next question"}]},
    ]


def test_provider_prepare_messages_drops_aborted_assistant_turn() -> None:
    adapter = OpenAIChatAdapter()
    request = cast(
        Any,
        _Obj(
            model="test-model",
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "text": "partial"}],
                    "meta": {"provider": "openai_chat", "model": "test-model", "stop_reason": "aborted"},
                },
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
        ),
    )

    assert adapter.prepare_messages(request) == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_provider_prepare_messages_downgrades_foreign_tool_thinking_to_text() -> None:
    adapter = OpenAIChatAdapter()
    request = cast(
        Any,
        _Obj(
            model="target-model",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need to inspect the file first."},
                        {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
                    ],
                    "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                }
            ],
        ),
    )

    assert adapter.prepare_messages(request) == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Need to inspect the file first."},
                {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
            ],
            "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "error: tool call was interrupted (no result recorded)",
                    "is_error": True,
                }
            ],
        },
    ]


def test_anthropic_prepare_messages_normalizes_tool_ids() -> None:
    adapter = AnthropicAdapter()
    request = cast(
        Any,
        _Obj(
            model="claude-sonnet-4-6",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "a/b", "name": "read", "input": {"path": "x.py"}},
                        {"type": "tool_use", "id": "a|b", "name": "write", "input": {"path": "y.py"}},
                    ],
                    "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "a/b", "content": "done a"},
                        {"type": "tool_result", "tool_use_id": "a|b", "content": "done b"},
                    ],
                },
            ],
        ),
    )

    prepared_messages = adapter.prepare_messages(request)
    assistant_blocks = prepared_messages[0]["content"]
    first_tool_id = assistant_blocks[0]["id"]
    second_tool_id = assistant_blocks[1]["id"]

    assert first_tool_id != second_tool_id
    assert first_tool_id.startswith("a_b_")
    assert second_tool_id.startswith("a_b_")
    assert prepared_messages == [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": first_tool_id, "name": "read", "input": {"path": "x.py"}},
                {"type": "tool_use", "id": second_tool_id, "name": "write", "input": {"path": "y.py"}},
            ],
            "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": first_tool_id, "content": "done a"},
                {"type": "tool_result", "tool_use_id": second_tool_id, "content": "done b"},
            ],
        },
    ]


def test_openai_chat_replays_reasoning_by_default() -> None:
    adapter = OpenAIChatAdapter()

    payload_messages = adapter._build_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "think", "meta": {"native": {"reasoning_field": "reasoning_content"}}},
                    {"type": "text", "text": "answer"},
                ],
            }
        ],
        system="",
    )

    assert payload_messages[0]["reasoning_content"] == "think"


def test_deepseek_only_replays_reasoning_during_tool_loop() -> None:
    adapter = DeepSeekAdapter()

    payload_messages = adapter._build_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "think", "meta": {"native": {"reasoning_field": "reasoning_content"}}},
                    {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "done"}]},
        ],
        system="",
    )
    assert payload_messages[0]["reasoning_content"] == "think"

    payload_messages = adapter._build_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "think", "meta": {"native": {"reasoning_field": "reasoning_content"}}},
                    {"type": "text", "text": "done"},
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "next question"}]},
        ],
        system="",
    )
    assert "reasoning_content" not in payload_messages[0]


def test_anthropic_replays_native_block_metadata() -> None:
    adapter = AnthropicAdapter()

    payload = adapter._serialize_message(
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "text": "think", "meta": {"native": {"signature": "sig_1"}}},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read",
                    "input": {},
                    "meta": {"native": {"caller": "server"}},
                },
            ],
        }
    )

    assert payload == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "think", "signature": "sig_1"},
            {"type": "tool_use", "id": "call_1", "name": "read", "input": {}, "caller": "server"},
        ],
    }


def test_anthropic_replays_thinking_without_signature() -> None:
    adapter = MoonshotAIAdapter()

    payload = adapter._serialize_message(
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "text": "Need the tool result first."},
                {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
            ],
        }
    )

    assert payload == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Need the tool result first."},
            {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
        ],
    }


def test_openai_compatible_provider_payload_overrides() -> None:
    request = cast(
        Any,
        _Obj(
            model="test-model",
            max_tokens=2048,
            system="",
            tools=[],
            reasoning_effort="high",
            messages=[],
        ),
    )

    assert OpenAIResponsesAdapter().supports_reasoning_effort is True

    openai_chat_payload = OpenAIChatAdapter()._build_request_payload(request)
    assert "reasoning_effort" not in openai_chat_payload

    deepseek_payload = DeepSeekAdapter()._build_request_payload(request)
    assert "extra_body" not in deepseek_payload

    zai_payload = ZAIAdapter()._build_request_payload(request)
    assert "extra_body" not in zai_payload

    openrouter_payload = OpenRouterAdapter()._build_request_payload(request)
    assert openrouter_payload["extra_body"] == {"reasoning": {"effort": "high"}}


def test_toggle_reasoning_payloads_disable_cleanly() -> None:
    request = cast(
        Any,
        _Obj(
            model="test-model",
            max_tokens=2048,
            system="",
            tools=[],
            reasoning_effort="none",
            messages=[],
        ),
    )

    assert "extra_body" not in DeepSeekAdapter()._build_request_payload(request)
    assert "extra_body" not in ZAIAdapter()._build_request_payload(request)
    assert OpenRouterAdapter()._build_request_payload(request)["extra_body"] == {"reasoning": {"effort": "none"}}


def test_anthropic_build_request_payload_includes_reasoning_config() -> None:
    adapter = AnthropicAdapter()
    request = cast(
        Any,
        _Obj(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[],
            system="",
            tools=[],
            reasoning_effort="high",
        ),
    )

    payload = adapter.build_request_payload(request)

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}


def test_anthropic_build_request_payload_maps_xhigh_and_4_5_correctly() -> None:
    adapter = AnthropicAdapter()

    request = cast(
        Any,
        _Obj(
            model="claude-opus-4-5",
            max_tokens=8192,
            messages=[],
            system="",
            tools=[],
            reasoning_effort="xhigh",
        ),
    )

    payload = adapter.build_request_payload(request)

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 32768}
    assert "output_config" not in payload
    assert adapter.supports_reasoning_effort is True


def test_anthropic_build_request_payload_maps_xhigh_for_opus_4_6() -> None:
    adapter = AnthropicAdapter()
    request = cast(
        Any,
        _Obj(
            model="claude-opus-4-6",
            max_tokens=8192,
            messages=[],
            system="",
            tools=[],
            reasoning_effort="xhigh",
        ),
    )

    payload = adapter.build_request_payload(request)

    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "max"}


def test_anthropic_like_build_request_payload_adds_cache_control() -> None:
    adapters = [AnthropicAdapter(), MoonshotAIAdapter(), MiniMaxAdapter()]

    for adapter in adapters:
        request = cast(
            Any,
            _Obj(
                model="test-model",
                max_tokens=4096,
                system="You are helpful.",
                tools=[],
                reasoning_effort=None,
                messages=[
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "first user message"}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "assistant reply"}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "latest user message"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": "tool output",
                                "is_error": False,
                            },
                        ],
                    },
                ],
            ),
        )

        payload = adapter.build_request_payload(request)

        assert payload["system"] == [
            {
                "type": "text",
                "text": "You are helpful.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert "cache_control" not in payload["messages"][0]["content"][0]
        assert payload["messages"][2]["content"][1]["cache_control"] == {"type": "ephemeral"}
