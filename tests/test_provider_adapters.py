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
from mycode.core.tools import TOOLS


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
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ]
        ),
    )

    input_items, previous_response_id = adapter._build_input_items(request)

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
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                    "meta": {"provider": "openai", "provider_message_id": "resp_123"},
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"}],
                },
            ]
        ),
    )

    input_items, previous_response_id = adapter._build_input_items(request)

    assert previous_response_id == "resp_123"
    assert input_items == [{"type": "function_call_output", "call_id": "call_1", "output": "file contents"}]


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
    assert message["content"][1] == {"type": "text", "text": "answer"}
    assert message["content"][2]["type"] == "tool_use"
    assert message["content"][2]["id"] == "call_1"
    assert message["content"][2]["input"] == {"path": "x.py"}


def test_openai_responses_serializes_strict_tool_schemas() -> None:
    adapter = OpenAIResponsesAdapter()

    serialized_tools = [adapter._serialize_tool(tool) for tool in TOOLS]

    for tool in serialized_tools:
        parameters = tool["parameters"]
        assert tool["strict"] is True
        assert parameters["required"] == list(parameters["properties"].keys())

    read_tool = next(tool for tool in serialized_tools if tool["name"] == "read")
    assert read_tool["parameters"]["properties"]["offset"]["type"] == ["integer", "null"]
    assert read_tool["parameters"]["properties"]["limit"]["type"] == ["integer", "null"]

    bash_tool = next(tool for tool in serialized_tools if tool["name"] == "bash")
    assert bash_tool["parameters"]["properties"]["timeout"]["type"] == ["integer", "null"]

    read_schema = next(tool for tool in TOOLS if tool["name"] == "read")["input_schema"]
    assert read_schema["required"] == ["path"]


def test_openai_chat_extracts_reasoning_from_known_extra_fields() -> None:
    adapter = OpenAIChatAdapter()

    delta = _Obj(reasoning_content="step zero")
    text, meta = adapter._extract_reasoning_delta(delta)
    assert text == "step zero"
    assert meta == {"openai_reasoning_field": "reasoning_content"}

    delta = _Obj(model_extra={"reasoning_content": "step one"})
    text, meta = adapter._extract_reasoning_delta(delta)
    assert text == "step one"
    assert meta == {"openai_reasoning_field": "reasoning_content"}

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
    assert meta["openai_reasoning_field"] == "reasoning_details"


def test_openai_chat_replays_reasoning_by_default() -> None:
    adapter = OpenAIChatAdapter()

    payload_messages = adapter._build_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "think", "meta": {"openai_reasoning_field": "reasoning_content"}},
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
                    {"type": "thinking", "text": "think", "meta": {"openai_reasoning_field": "reasoning_content"}},
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
                    {"type": "thinking", "text": "think", "meta": {"openai_reasoning_field": "reasoning_content"}},
                    {"type": "text", "text": "done"},
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": "next question"}]},
        ],
        system="",
    )
    assert "reasoning_content" not in payload_messages[0]


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
