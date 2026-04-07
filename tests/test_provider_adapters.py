from __future__ import annotations

import base64
from typing import Any, cast

import pytest

from mycode.core.providers import (
    AnthropicAdapter,
    DeepSeekAdapter,
    GoogleGeminiAdapter,
    MiniMaxAdapter,
    MoonshotAIAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    OpenRouterAdapter,
    ZAIAdapter,
)
from mycode.core.providers.base import ProviderStreamEvent, repair_messages_for_replay
from mycode.core.tools import DEFAULT_TOOL_SPECS

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1X8AAAAASUVORK5CYII="
)


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        def _dump(value):
            if hasattr(value, "model_dump"):
                return value.model_dump()
            if isinstance(value, list):
                return [_dump(item) for item in value]
            if isinstance(value, dict):
                return {key: _dump(item) for key, item in value.items()}
            return value

        return {key: _dump(value) for key, value in self.__dict__.items()}


def test_openai_responses_builds_initial_input_items() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id=None,
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ],
            system="",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert input_items == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


def test_openai_responses_serializes_user_image_input(tmp_path) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(_PNG_1X1)
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id=None,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image",
                            "data": base64.b64encode(image_path.read_bytes()).decode("utf-8"),
                            "mime_type": "image/png",
                        },
                    ],
                }
            ],
            system="",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert input_items[0]["role"] == "user"
    assert input_items[0]["content"][0] == {"type": "input_text", "text": "describe"}
    assert input_items[0]["content"][1]["type"] == "input_image"
    assert input_items[0]["content"][1]["image_url"].startswith("data:image/png;base64,")


def test_openai_responses_replays_native_output_items_for_tool_results() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id=None,
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                    "meta": {
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "native": {
                            "output_items": [
                                {
                                    "type": "reasoning",
                                    "id": "rs_1",
                                    "status": "completed",
                                    "summary": [],
                                    "encrypted_content": "enc_1",
                                },
                                {
                                    "type": "message",
                                    "id": "msg_1",
                                    "role": "assistant",
                                    "phase": "commentary",
                                    "status": "completed",
                                    "content": [{"type": "output_text", "text": "Checking the file."}],
                                },
                                {
                                    "type": "function_call",
                                    "id": "fc_1",
                                    "call_id": "call_1",
                                    "name": "read",
                                    "arguments": '{"path": "x.py"}',
                                    "status": "completed",
                                },
                            ]
                        },
                    },
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "file contents",
                            "display_text": "file contents",
                        }
                    ],
                },
            ],
            system="",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert input_items == [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc_1"},
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Checking the file."}],
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read",
            "arguments": '{"path": "x.py"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
    ]


def test_openai_responses_serializes_tool_result_images(tmp_path) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(_PNG_1X1)
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id=None,
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.png"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "Read image file [image/png]",
                            "display_text": "Read image file [image/png]",
                            "content": [
                                {"type": "text", "text": "Read image file [image/png]"},
                                {
                                    "type": "image",
                                    "data": base64.b64encode(image_path.read_bytes()).decode("utf-8"),
                                    "mime_type": "image/png",
                                },
                            ],
                        }
                    ],
                },
            ],
            system="",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert input_items[0] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "read",
        "arguments": '{"path": "x.png"}',
    }
    assert input_items[1]["type"] == "function_call_output"
    assert input_items[1]["call_id"] == "call_1"
    assert input_items[1]["output"][0] == {"type": "input_text", "text": "Read image file [image/png]"}
    assert input_items[1]["output"][1]["type"] == "input_image"
    assert input_items[1]["output"][1]["image_url"].startswith("data:image/png;base64,")


def test_openai_responses_falls_back_to_full_replay_for_cross_provider_history() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id=None,
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
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "42",
                            "display_text": "42",
                        }
                    ],
                },
            ],
            system="",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert input_items == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "double 21"}],
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


def test_anthropic_serializes_image_tool_result_content(tmp_path) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(_PNG_1X1)
    adapter = AnthropicAdapter()

    payload = adapter._build_request_payload(
        cast(
            Any,
            _Obj(
                model="claude-sonnet-4-6",
                messages=[
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.png"}}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "model_text": "Read image file [image/png]",
                                "display_text": "Read image file [image/png]",
                                "content": [
                                    {"type": "text", "text": "Read image file [image/png]"},
                                    {
                                        "type": "image",
                                        "data": base64.b64encode(image_path.read_bytes()).decode("utf-8"),
                                        "mime_type": "image/png",
                                    },
                                ],
                            }
                        ],
                    },
                ],
                system="",
                tools=[],
                max_tokens=4096,
                reasoning_effort=None,
                api_key=None,
                api_base=None,
                session_id=None,
            ),
        )
    )

    content = payload["messages"][1]["content"][0]["content"]
    assert content[0] == {"type": "text", "text": "Read image file [image/png]"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"


def test_openai_responses_fallback_replay_skips_reasoning_blocks() -> None:
    adapter = OpenAIResponsesAdapter()
    request = cast(
        Any,
        _Obj(
            model="gpt-5.4",
            session_id=None,
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need the tool first."},
                        {"type": "text", "text": "I will inspect the file."},
                        {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
                    ],
                    "meta": {"provider": "openai", "model": "gpt-5.4"},
                },
            ],
            system="",
            tools=[],
            max_tokens=4096,
            reasoning_effort=None,
        ),
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert input_items == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will inspect the file."}],
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
            "output": "error: tool call was interrupted",
        },
    ]


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
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in payload


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
    assert message["meta"]["native"]["output_items"] == [
        {"type": "reasoning", "id": "rs_1", "status": "completed", "content": [{"text": "think"}], "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": "answer", "annotations": []}]},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read",
            "arguments": '{"path": "x.py"}',
            "status": "completed",
        },
    ]


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


def test_google_gemini_builds_initial_contents() -> None:
    adapter = GoogleGeminiAdapter()
    request = cast(
        Any,
        _Obj(
            model="gemini-3-flash-preview",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ],
        ),
    )

    assert adapter._build_contents(request) == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_google_gemini_serializes_user_image_input() -> None:
    adapter = GoogleGeminiAdapter()
    request = cast(
        Any,
        _Obj(
            model="gemini-3-flash-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image", "data": "YWJj", "mime_type": "image/png"},
                    ],
                }
            ],
        ),
    )

    assert adapter._build_contents(request) == [
        {
            "role": "user",
            "parts": [
                {"text": "describe"},
                {"inline_data": {"mime_type": "image/png", "data": "YWJj"}},
            ],
        }
    ]


def test_google_gemini_falls_back_to_full_replay_for_cross_provider_history() -> None:
    adapter = GoogleGeminiAdapter()
    request = cast(
        Any,
        _Obj(
            model="gemini-3-flash-preview",
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "double 21"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need the tool first."},
                        {"type": "text", "text": "I will inspect the file."},
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
                            "model_text": "42",
                            "display_text": "42",
                        }
                    ],
                },
            ],
        ),
    )

    assert adapter._build_contents(request) == [
        {"role": "user", "parts": [{"text": "double 21"}]},
        {
            "role": "model",
            "parts": [
                {"text": "Need the tool first.", "thought": True},
                {"text": "I will inspect the file."},
                {
                    "function_call": {"id": "call_1", "name": "read", "args": {"path": "x.py"}},
                    "thought_signature": "skip_thought_signature_validator",
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "call_1",
                        "name": "read",
                        "response": {"result": "42"},
                    }
                }
            ],
        },
    ]


def test_google_gemini_replays_native_parts_for_same_provider_history() -> None:
    adapter = GoogleGeminiAdapter()
    request = cast(
        Any,
        _Obj(
            model="gemini-3-flash-preview",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "text": "Think",
                            "meta": {
                                "native": {
                                    "part": {
                                        "text": "Think",
                                        "thought": True,
                                        "thought_signature": "c2ln",
                                    }
                                }
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "read",
                            "input": {"path": "x.py"},
                            "meta": {
                                "native": {
                                    "part": {
                                        "function_call": {
                                            "id": "call_1",
                                            "name": "read",
                                            "args": {"path": "x.py"},
                                        },
                                        "thought_signature": "c2ln",
                                    }
                                }
                            },
                        },
                    ],
                    "meta": {"provider": "google", "model": "gemini-3-flash-preview"},
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "file contents",
                            "display_text": "file contents",
                        }
                    ],
                },
            ],
        ),
    )

    assert adapter._build_contents(request) == [
        {
            "role": "model",
            "parts": [
                {"text": "Think", "thought": True, "thought_signature": "c2ln"},
                {
                    "function_call": {"id": "call_1", "name": "read", "args": {"path": "x.py"}},
                    "thought_signature": "c2ln",
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "call_1",
                        "name": "read",
                        "response": {"result": "file contents"},
                    }
                }
            ],
        },
    ]


def test_google_gemini_build_request_config_maps_reasoning_effort() -> None:
    adapter = GoogleGeminiAdapter()

    pro_request = cast(
        Any,
        _Obj(
            model="gemini-3.1-pro-preview",
            system="You are helpful.",
            tools=[],
            max_tokens=2048,
            reasoning_effort="none",
        ),
    )
    flash_request = cast(
        Any,
        _Obj(
            model="gemini-3-flash-preview",
            system="You are helpful.",
            tools=[],
            max_tokens=2048,
            reasoning_effort="none",
        ),
    )

    pro_config = adapter._build_config(pro_request).model_dump(mode="json", exclude_none=True)
    flash_config = adapter._build_config(flash_request).model_dump(mode="json", exclude_none=True)

    assert pro_config["thinking_config"] == {"include_thoughts": True, "thinking_level": "LOW"}
    assert flash_config["thinking_config"] == {"include_thoughts": True, "thinking_level": "MINIMAL"}


def test_google_gemini_streaming_parts_merge_into_final_blocks() -> None:
    adapter = GoogleGeminiAdapter()
    blocks: list[dict[str, Any]] = []

    events = adapter._consume_part(
        blocks,
        _Obj(text="step ", thought=True, thought_signature=None, function_call=None),
    )
    assert events == [ProviderStreamEvent("thinking_delta", {"text": "step "})]

    events = adapter._consume_part(
        blocks,
        _Obj(text="one", thought=True, thought_signature="c2ln", function_call=None),
    )
    assert events == [ProviderStreamEvent("thinking_delta", {"text": "one"})]

    events = adapter._consume_part(
        blocks,
        _Obj(
            text=None,
            thought=False,
            thought_signature="c2ln",
            function_call=_Obj(id="call_1", name="read", args={"path": "x.py"}),
        ),
    )
    assert events == []
    assert blocks == [
        {
            "type": "thinking",
            "text": "step one",
            "meta": {"native": {"part": {"text": "step one", "thought": True, "thought_signature": "c2ln"}}},
        },
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read",
            "input": {"path": "x.py"},
            "meta": {
                "native": {
                    "part": {
                        "function_call": {"id": "call_1", "name": "read", "args": {"path": "x.py"}},
                        "thought_signature": "c2ln",
                    }
                }
            },
        },
    ]


def test_google_gemini_keeps_signature_only_stream_chunk() -> None:
    adapter = GoogleGeminiAdapter()
    blocks: list[dict[str, Any]] = []

    events = adapter._consume_part(
        blocks,
        _Obj(text="", thought=False, thought_signature="c2ln", function_call=None),
    )

    assert events == []
    assert blocks == [
        {
            "type": "text",
            "text": "",
            "meta": {"native": {"part": {"text": "", "thought_signature": "c2ln"}}},
        }
    ]


@pytest.mark.parametrize(
    ("delta", "expected_text", "expected_meta"),
    [
        (
            _Obj(reasoning_content="step zero"),
            "step zero",
            {"reasoning_field": "reasoning_content"},
        ),
        (
            _Obj(model_extra={"reasoning_content": "step one"}),
            "step one",
            {"reasoning_field": "reasoning_content"},
        ),
        (
            _Obj(
                model_extra={
                    "reasoning_details": [
                        {"type": "reasoning.text", "text": "step "},
                        {"type": "reasoning.text", "text": "two"},
                    ]
                }
            ),
            "step two",
            {
                "reasoning_field": "reasoning_details",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "step "},
                    {"type": "reasoning.text", "text": "two"},
                ],
            },
        ),
    ],
)
def test_openai_chat_extracts_reasoning_from_known_extra_fields(delta, expected_text, expected_meta) -> None:
    adapter = OpenAIChatAdapter()
    text, meta = adapter._extract_reasoning_delta(delta)

    assert text == expected_text
    assert meta == expected_meta


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need to inspect the file first."},
                        {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
                    ],
                    "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                }
            ],
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need to inspect the file first."},
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
                            "model_text": "error: tool call was interrupted",
                            "display_text": "Tool call was interrupted",
                            "is_error": True,
                        }
                    ],
                },
            ],
            id="closes-interrupted-tool-loop",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "text": "partial"}],
                    "meta": {"provider": "openai_chat", "model": "test-model", "stop_reason": "aborted"},
                },
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
            [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            id="drops-aborted-assistant-turn",
        ),
        pytest.param(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "first",
                            "display_text": "first",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "duplicate",
                            "display_text": "duplicate",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_2",
                            "model_text": "orphan",
                            "display_text": "orphan",
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                },
            ],
            [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "first",
                            "display_text": "first",
                        }
                    ],
                },
            ],
            id="drops-duplicate-and-orphan-tool-records",
        ),
        pytest.param(
            [
                {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "missing",
                            "model_text": "orphan",
                            "display_text": "orphan",
                        }
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
            ],
            [
                {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "[User turn omitted during replay]"}],
                    "meta": {"synthetic": True},
                },
                {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
            ],
            id="keeps-placeholder-user-turn",
        ),
    ],
)
def test_repair_messages_for_replay(messages, expected) -> None:
    assert repair_messages_for_replay(messages, supports_image_input=True) == expected


def test_provider_prepare_messages_filters_history_images_when_disabled() -> None:
    adapter = OpenAIChatAdapter()
    request = cast(
        Any,
        _Obj(
            model="test-model",
            supports_image_input=False,
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.png"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image", "data": "abc", "mime_type": "image/png"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "model_text": "Read image file [image/png]",
                            "display_text": "Read image file [image/png]",
                            "content": [
                                {"type": "text", "text": "Read image file [image/png]"},
                                {"type": "image", "data": "abc", "mime_type": "image/png"},
                            ],
                        },
                    ],
                },
            ],
        ),
    )

    assert adapter.prepare_messages(request) == [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.png"}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "text",
                    "text": '<file name="attached-image" media_type="image/png" kind="image">Current model does not support image input.</file>',
                    "meta": {"attachment": True},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "model_text": "Read image file [image/png]",
                    "display_text": "Read image file [image/png]",
                    "content": [{"type": "text", "text": "Read image file [image/png]"}],
                },
            ],
        },
    ]


def test_provider_prepare_messages_replaces_user_images_with_text_notice_when_disabled() -> None:
    adapter = OpenAIChatAdapter()
    request = cast(
        Any,
        _Obj(
            model="test-model",
            supports_image_input=False,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "check this"},
                        {"type": "image", "data": "abc", "mime_type": "image/png", "name": "logo.png"},
                    ],
                }
            ],
        ),
    )

    assert adapter.prepare_messages(request) == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "check this"},
                {
                    "type": "text",
                    "text": '<file name="logo.png" media_type="image/png" kind="image">Current model does not support image input.</file>',
                    "meta": {"attachment": True},
                },
            ],
        }
    ]


def test_provider_prepare_messages_escapes_image_notice_attributes_when_disabled() -> None:
    adapter = OpenAIChatAdapter()
    request = cast(
        Any,
        _Obj(
            model="test-model",
            supports_image_input=False,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "data": "abc",
                            "mime_type": 'image/"png"',
                            "name": 'logo"<v2>.png',
                        },
                    ],
                }
            ],
        ),
    )

    assert adapter.prepare_messages(request) == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": '<file name="logo&quot;&lt;v2&gt;.png" media_type="image/&quot;png&quot;" kind="image">Current model does not support image input.</file>',
                    "meta": {"attachment": True},
                },
            ],
        }
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
                        {
                            "type": "tool_result",
                            "tool_use_id": "a/b",
                            "model_text": "done a",
                            "display_text": "done a",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "a|b",
                            "model_text": "done b",
                            "display_text": "done b",
                        },
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
                {
                    "type": "tool_result",
                    "tool_use_id": first_tool_id,
                    "model_text": "done a",
                    "display_text": "done a",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": second_tool_id,
                    "model_text": "done b",
                    "display_text": "done b",
                },
            ],
        },
    ]


def test_openai_chat_replays_reasoning_by_default() -> None:
    adapter = OpenAIChatAdapter()

    payload_messages = adapter._build_request_payload(
        cast(
            Any,
            _Obj(
                model="test-model",
                max_tokens=2048,
                system="",
                tools=[],
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "text": "think"},
                            {"type": "text", "text": "answer"},
                        ],
                    }
                ],
            ),
        )
    )["messages"]

    assert payload_messages[0]["reasoning_content"] == "think"


def test_openai_chat_serializes_user_image_input() -> None:
    adapter = OpenAIChatAdapter()

    payload_messages = adapter._build_request_payload(
        cast(
            Any,
            _Obj(
                model="test-model",
                max_tokens=2048,
                system="",
                tools=[],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {"type": "image", "data": "YWJj", "mime_type": "image/png"},
                        ],
                    }
                ],
            ),
        )
    )["messages"]

    assert payload_messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}},
            ],
        }
    ]


def test_deepseek_replays_reasoning_across_turns() -> None:
    adapter = DeepSeekAdapter()

    payload_messages = adapter._build_request_payload(
        cast(
            Any,
            _Obj(
                model="test-model",
                max_tokens=2048,
                system="",
                tools=[],
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "text": "think",
                                "meta": {"native": {"reasoning_field": "reasoning_content"}},
                            },
                            {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "model_text": "done",
                                "display_text": "done",
                            }
                        ],
                    },
                ],
            ),
        )
    )["messages"]
    assert payload_messages[0]["reasoning_content"] == "think"
    payload_messages = adapter._build_request_payload(
        cast(
            Any,
            _Obj(
                model="test-model",
                max_tokens=2048,
                system="",
                tools=[],
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "text": "think",
                                "meta": {"native": {"reasoning_field": "reasoning_content"}},
                            },
                            {"type": "text", "text": "done"},
                        ],
                    },
                    {"role": "user", "content": [{"type": "text", "text": "next question"}]},
                ],
            ),
        )
    )["messages"]
    assert payload_messages[0]["reasoning_content"] == "think"


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


@pytest.mark.parametrize("adapter", [MoonshotAIAdapter(), AnthropicAdapter()])
def test_anthropic_like_replays_unsigned_thinking_without_signature(adapter) -> None:
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
    assert zai_payload["extra_body"] == {"thinking": {"type": "enabled", "clear_thinking": False}}

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
    assert ZAIAdapter()._build_request_payload(request)["extra_body"] == {
        "thinking": {"type": "enabled", "clear_thinking": False}
    }
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

    payload = adapter._build_request_payload(request)

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

    payload = adapter._build_request_payload(request)

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

    payload = adapter._build_request_payload(request)

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
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "latest user message"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "model_text": "tool output",
                                "display_text": "tool output",
                                "is_error": False,
                            },
                        ],
                    },
                ],
            ),
        )

        payload = adapter._build_request_payload(request)

        assert payload["system"] == [
            {
                "type": "text",
                "text": "You are helpful.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert "cache_control" not in payload["messages"][0]["content"][0]
        assert payload["messages"][3]["content"][1]["cache_control"] == {"type": "ephemeral"}
