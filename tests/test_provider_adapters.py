from __future__ import annotations

import base64
import json
from typing import Any, Literal, cast

import pytest

from mycode.compact import build_compact_event
from mycode.providers import (
    AnthropicAdapter,
    DeepSeekAdapter,
    GoogleGeminiAdapter,
    MiniMaxAdapter,
    MoonshotAIAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    OpenRouterAdapter,
    XAIAdapter,
)
from mycode.providers.base import repair_messages_for_replay
from mycode.tools import tool as define_tool

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1X8AAAAASUVORK5CYII="
)
_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


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


def request_obj(**overrides: Any) -> Any:
    data = {
        "model": "test-model",
        "session_id": None,
        "messages": [],
        "system": "",
        "tools": [],
        "max_tokens": 4096,
        "temperature": 1.0,
        "reasoning_effort": None,
        "api_key": None,
        "api_base": None,
        "supports_image_input": True,
        "supports_pdf_input": True,
        "transcript_path": None,
        "append_messages": [],
    }
    data.update(overrides)
    return cast(Any, _Obj(**data))


# User media block wire shapes


@pytest.mark.parametrize(
    ("adapter", "payload_builder", "expected_image_type"),
    [
        pytest.param(
            OpenAIResponsesAdapter(),
            lambda adapter, request: adapter._build_request_payload(request)["input"][0]["content"],
            "input_image",
            id="openai-responses",
        ),
        pytest.param(
            GoogleGeminiAdapter(),
            lambda adapter, request: adapter._build_contents(request)[0]["parts"],
            "inline_data",
            id="gemini",
        ),
        pytest.param(
            OpenAIChatAdapter(),
            lambda adapter, request: adapter._build_request_payload(request)["messages"][0]["content"],
            "image_url",
            id="openai-chat",
        ),
    ],
)
def test_user_image_input_serialization(
    tmp_path,
    adapter: Any,
    payload_builder: Any,
    expected_image_type: str,
) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(_PNG_1X1)
    image_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    request = request_obj(
        model="gpt-5.4",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image", "data": image_data, "mime_type": "image/png"},
                ],
            }
        ],
    )

    content = payload_builder(adapter, request)

    if expected_image_type == "input_image":
        assert content[0] == {"type": "input_text", "text": "describe"}
        assert content[1]["type"] == "input_image"
        assert content[1]["image_url"].startswith("data:image/png;base64,")
    elif expected_image_type == "inline_data":
        assert content[0] == {"text": "describe"}
        assert content[1]["inline_data"] == {"mime_type": "image/png", "data": image_data}
    else:
        assert content[0] == {"type": "text", "text": "describe"}
        assert content[1]["image_url"] == {"url": f"data:image/png;base64,{image_data}"}


@pytest.mark.parametrize(
    ("adapter", "payload_builder", "expected_kind"),
    [
        pytest.param(
            OpenAIResponsesAdapter(),
            lambda adapter, request: adapter._build_request_payload(request)["input"][0]["content"],
            "input_file",
            id="openai-responses",
        ),
        pytest.param(
            OpenAIChatAdapter(),
            lambda adapter, request: adapter._build_request_payload(request)["messages"][0]["content"],
            "file",
            id="openai-chat",
        ),
        pytest.param(
            OpenRouterAdapter(),
            lambda adapter, request: adapter._build_request_payload(request)["messages"][0]["content"],
            "file",
            id="openrouter",
        ),
        pytest.param(
            AnthropicAdapter(),
            lambda adapter, request: adapter._build_request_payload(request)["messages"][0]["content"],
            "document",
            id="anthropic",
        ),
        pytest.param(
            GoogleGeminiAdapter(),
            lambda adapter, request: adapter._build_contents(request)[0]["parts"],
            "inline_data",
            id="gemini",
        ),
    ],
)
def test_user_pdf_input_serialization(
    adapter: Any,
    payload_builder: Any,
    expected_kind: str,
) -> None:
    pdf_data = base64.b64encode(_PDF_BYTES).decode("utf-8")
    request = request_obj(
        model="gpt-5.4",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "summarize"},
                    {
                        "type": "document",
                        "data": pdf_data,
                        "mime_type": "application/pdf",
                        "name": "report.pdf",
                    },
                ],
            }
        ],
    )

    content = payload_builder(adapter, request)

    if expected_kind == "input_file":
        assert content[0] == {"type": "input_text", "text": "summarize"}
        assert content[1] == {
            "type": "input_file",
            "filename": "report.pdf",
            "file_data": f"data:application/pdf;base64,{pdf_data}",
        }
    elif expected_kind == "file":
        assert content[0] == {"type": "text", "text": "summarize"}
        assert content[1] == {
            "type": "file",
            "file": {
                "filename": "report.pdf",
                "file_data": f"data:application/pdf;base64,{pdf_data}",
            },
        }
    elif expected_kind == "document":
        assert content[0] == {"type": "text", "text": "summarize"}
        assert content[1]["type"] == "document"
        assert content[1]["source"] == {
            "type": "base64",
            "media_type": "application/pdf",
            "data": pdf_data,
        }
    else:
        assert content[0] == {"text": "summarize"}
        assert content[1] == {"inline_data": {"mime_type": "application/pdf", "data": pdf_data}}


# Replay repair


def test_repair_messages_for_replay_downgrades_pdf_for_unsupported_models() -> None:
    replay = repair_messages_for_replay(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "check this"},
                    {
                        "type": "document",
                        "data": base64.b64encode(_PDF_BYTES).decode("utf-8"),
                        "mime_type": "application/pdf",
                        "name": 'report <"draft">.pdf',
                    },
                ],
            }
        ],
        supports_image_input=True,
        supports_pdf_input=False,
    )

    assert replay == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "check this"},
                {
                    "type": "text",
                    "text": '<file name="report &lt;&quot;draft&quot;&gt;.pdf" media_type="application/pdf" kind="document">Current model does not support PDF input.</file>',
                    "meta": {"attachment": True},
                },
            ],
        }
    ]


# OpenAI Responses adapter


def test_openai_responses_replays_native_output_items_for_tool_results() -> None:
    adapter = OpenAIResponsesAdapter()
    request = request_obj(
        model="gpt-5.4",
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
                        "output": "file contents",
                    }
                ],
            },
        ],
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert [item["type"] for item in input_items] == [
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert input_items[0]["encrypted_content"] == "enc_1"
    assert input_items[1]["content"][0]["text"] == "Checking the file."
    assert input_items[2]["call_id"] == "call_1"
    assert input_items[2]["arguments"] == '{"path": "x.py"}'
    assert input_items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "file contents"}


def test_openai_responses_serializes_tool_result_images(tmp_path) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(_PNG_1X1)
    adapter = OpenAIResponsesAdapter()
    request = request_obj(
        model="gpt-5.4",
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
                        "output": "Read image file [image/png]",
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
    request = request_obj(
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
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "output": "42",
                    }
                ],
            },
        ],
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert [item["type"] for item in input_items] == ["message", "function_call", "function_call_output"]
    assert input_items[0]["role"] == "user"
    assert input_items[0]["content"][0]["text"] == "double 21"
    assert input_items[1]["call_id"] == "call_1"
    assert input_items[1]["arguments"] == '{"path": "x.py"}'
    assert input_items[2]["output"] == "42"


def test_openai_responses_fallback_replay_skips_reasoning_blocks() -> None:
    adapter = OpenAIResponsesAdapter()
    request = request_obj(
        model="gpt-5.4",
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
    )

    input_items = adapter._build_request_payload(request)["input"]

    assert [item["type"] for item in input_items] == ["message", "function_call", "function_call_output"]
    assert input_items[0]["content"] == [{"type": "output_text", "text": "I will inspect the file."}]
    assert input_items[1]["call_id"] == "call_1"
    assert input_items[2]["output"] == "error: tool call was interrupted"


def test_openai_responses_build_request_payload_includes_prompt_cache_key() -> None:
    adapter = OpenAIResponsesAdapter()
    request = request_obj(
        model="gpt-5.4",
        session_id="session_123",
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
        system="You are helpful.",
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
            _Obj(type="reasoning", id="rs_1", status="completed", summary=[_Obj(text="think")]),
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
    assert message["content"][0]["meta"] == {
        "native": {"item_id": "rs_1", "status": "completed", "summary": [{"text": "think"}]}
    }
    assert message["content"][1] == {"type": "text", "text": "answer"}
    assert message["content"][2]["type"] == "tool_use"
    assert message["content"][2]["id"] == "call_1"
    assert message["content"][2]["input"] == {"path": "x.py"}
    assert message["content"][2]["meta"] == {"native": {"item_id": "fc_1", "status": "completed"}}
    native_items = message["meta"]["native"]["output_items"]
    assert [item["type"] for item in native_items] == ["reasoning", "message", "function_call"]
    assert native_items[0]["id"] == "rs_1"
    assert native_items[1]["content"][0]["text"] == "answer"
    assert native_items[2]["call_id"] == "call_1"


def test_openai_responses_uses_stream_output_items_when_final_output_is_empty() -> None:
    adapter = OpenAIResponsesAdapter()
    response = _Obj(
        id="resp_123",
        model="gpt-5.4",
        status="completed",
        usage=_Obj(input_tokens=10, output_tokens=5),
        output=[],
    )

    message = adapter._convert_final_response(
        response,
        output_items=[
            _Obj(type="message", content=[_Obj(type="output_text", text="hello world", annotations=[])]),
            _Obj(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="read",
                arguments='{"path":"pyproject.toml"}',
                status="completed",
            ),
        ],
    )

    assert message["role"] == "assistant"
    assert message["content"][0] == {"type": "text", "text": "hello world"}
    assert message["content"][1]["type"] == "tool_use"
    assert message["content"][1]["id"] == "call_1"
    assert message["content"][1]["input"] == {"path": "pyproject.toml"}
    assert message["content"][1]["meta"] == {"native": {"item_id": "fc_1", "status": "completed"}}


def test_openai_responses_preserves_invalid_tool_arguments() -> None:
    adapter = OpenAIResponsesAdapter()
    response = _Obj(
        id="resp_123",
        model="gpt-5.4",
        status="completed",
        usage=_Obj(input_tokens=10, output_tokens=5),
        output=[
            _Obj(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="read",
                arguments="{not json",
                status="completed",
            ),
        ],
    )

    message = adapter._convert_final_response(response)

    assert message["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read",
            "input": {},
            "meta": {"native": {"item_id": "fc_1", "status": "completed", "raw_arguments": "{not json"}},
        }
    ]


def test_openai_responses_serializes_strict_tool_schemas() -> None:
    adapter = OpenAIResponsesAdapter()
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
            "mode": {"type": "string", "enum": ["fast", "safe"], "default": "fast"},
            "edits": {"type": "array", "items": {"$ref": "#/$defs/EditEntry"}},
        },
        "required": ["path", "edits"],
        "$defs": {
            "EditEntry": {
                "type": "object",
                "properties": {
                    "oldText": {"type": "string"},
                    "newText": {"type": "string"},
                },
                "required": ["oldText", "newText"],
            }
        },
    }

    payload = adapter._build_request_payload(
        request_obj(
            model="gpt-5.4",
            messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            tools=[
                {
                    "name": "patch",
                    "description": "Patch a file.",
                    "input_schema": input_schema,
                }
            ],
        )
    )

    serialized_tool = payload["tools"][0]
    parameters = serialized_tool["parameters"]
    assert serialized_tool["strict"] is True
    assert parameters["required"] == list(parameters["properties"].keys())
    assert parameters["additionalProperties"] is False

    assert parameters["properties"]["limit"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert "default" not in parameters["properties"]["limit"]

    assert parameters["properties"]["mode"]["type"] == ["string", "null"]
    assert parameters["properties"]["mode"]["enum"] == ["fast", "safe", None]
    assert "default" not in parameters["properties"]["mode"]

    edit_entry = parameters["$defs"]["EditEntry"]
    assert edit_entry["additionalProperties"] is False
    assert edit_entry["required"] == ["oldText", "newText"]


def test_openai_responses_allows_null_for_optional_literal_tool_fields() -> None:
    @define_tool
    def choose_enum(mode: Literal["fast", "safe"] = "fast") -> str:
        """Choose a mode."""

        return mode

    @define_tool
    def choose_const(mode: Literal["fast"] = "fast") -> str:
        """Choose a mode."""

        return mode

    for literal_tool, expected_enum in [(choose_enum, ["fast", "safe", None]), (choose_const, ["fast", None])]:
        payload = OpenAIResponsesAdapter()._build_request_payload(
            request_obj(
                model="gpt-5.4",
                messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                tools=[
                    {
                        "name": literal_tool.name,
                        "description": literal_tool.description,
                        "input_schema": literal_tool.input_schema,
                    }
                ],
            )
        )

        mode_schema = payload["tools"][0]["parameters"]["properties"]["mode"]
        assert mode_schema["type"] == ["string", "null"]
        assert mode_schema["enum"] == expected_enum
        assert "const" not in mode_schema


def test_openai_responses_rejects_dynamic_object_schemas() -> None:
    with pytest.raises(ValueError, match="dynamic keys"):
        OpenAIResponsesAdapter()._build_request_payload(
            request_obj(
                model="gpt-5.4",
                messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                tools=[
                    {
                        "name": "search",
                        "description": "Search entries.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "filters": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                }
                            },
                            "required": ["filters"],
                        },
                    }
                ],
            )
        )


# Gemini adapter


def test_google_gemini_falls_back_to_full_replay_for_cross_provider_history() -> None:
    adapter = GoogleGeminiAdapter()
    request = request_obj(
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
                        "output": "42",
                    }
                ],
            },
        ],
    )

    contents = adapter._build_contents(request)

    assert [content["role"] for content in contents] == ["user", "model", "user"]
    assert contents[0]["parts"][0]["text"] == "double 21"
    assert contents[1]["parts"][0] == {"text": "Need the tool first.", "thought": True}
    assert contents[1]["parts"][2]["function_call"] == {"id": "call_1", "name": "read", "args": {"path": "x.py"}}
    assert contents[1]["parts"][2]["thought_signature"] == "skip_thought_signature_validator"
    assert contents[2]["parts"][0]["function_response"]["response"] == {"result": "42"}


def test_google_gemini_replays_native_parts_for_same_provider_history() -> None:
    adapter = GoogleGeminiAdapter()
    request = request_obj(
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
                        "output": "file contents",
                    }
                ],
            },
        ],
    )

    contents = adapter._build_contents(request)

    assert [content["role"] for content in contents] == ["model", "user"]
    assert contents[0]["parts"][0] == {"text": "Think", "thought": True, "thought_signature": "c2ln"}
    assert contents[0]["parts"][1]["function_call"] == {"id": "call_1", "name": "read", "args": {"path": "x.py"}}
    assert contents[0]["parts"][1]["thought_signature"] == "c2ln"
    assert contents[1]["parts"][0]["function_response"]["response"] == {"result": "file contents"}


def test_google_gemini_build_request_config_uses_supported_tool_settings() -> None:
    adapter = GoogleGeminiAdapter()
    request = request_obj(
        model="gemini-3-flash-preview",
        system="You are helpful.",
        tools=[
            {
                "name": "read",
                "description": "Read a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
        max_tokens=2048,
    )

    config = adapter._build_config(request).model_dump(mode="json", exclude_none=True)

    tool = config["tools"][0]["function_declarations"][0]

    assert tool["name"] == "read"
    assert tool["parameters_json_schema"]["required"] == ["path"]
    assert "tool_config" not in config
    assert "automatic_function_calling" not in config


# Provider replay preparation


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
                            "output": "error: tool call was interrupted",
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
                            "output": "first",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "output": "duplicate",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_2",
                            "output": "orphan",
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
                            "output": "first",
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
                            "output": "orphan",
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
    assert (
        repair_messages_for_replay(
            messages,
            supports_image_input=True,
            supports_pdf_input=True,
        )
        == expected
    )


def test_provider_prepare_messages_filters_history_images_when_disabled() -> None:
    adapter = OpenAIChatAdapter()
    request = request_obj(
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
                        "output": "Read image file [image/png]",
                        "content": [
                            {"type": "text", "text": "Read image file [image/png]"},
                            {"type": "image", "data": "abc", "mime_type": "image/png"},
                        ],
                    },
                ],
            },
        ],
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
                    "output": "Read image file [image/png]",
                    "content": [{"type": "text", "text": "Read image file [image/png]"}],
                },
            ],
        },
    ]


def test_provider_prepare_messages_applies_compact_before_appending_messages() -> None:
    adapter = OpenAIChatAdapter()
    request = request_obj(
        transcript_path="/sessions/s1/messages.jsonl",
        append_messages=[{"role": "user", "content": [{"type": "text", "text": "summarize now"}]}],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "old prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            build_compact_event("latest summary", provider="openai", model="gpt-5.4"),
            {"role": "assistant", "content": [{"type": "text", "text": "tail answer"}]},
        ],
    )

    prepared = adapter.prepare_messages(request)

    assert [message["role"] for message in prepared] == ["user", "assistant", "user"]
    assert "latest summary" in prepared[0]["content"][0]["text"]
    assert "/sessions/s1/messages.jsonl" in prepared[0]["content"][0]["text"]
    assert "old prompt" not in prepared[0]["content"][0]["text"]
    assert prepared[1]["content"][0]["text"] == "tail answer"
    assert prepared[2]["content"][0]["text"] == "summarize now"


def test_provider_prepare_messages_escapes_image_notice_attributes_when_disabled() -> None:
    adapter = OpenAIChatAdapter()
    request = request_obj(
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
    request = request_obj(
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
                        "output": "done a",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "a|b",
                        "output": "done b",
                    },
                ],
            },
        ],
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
                    "output": "done a",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": second_tool_id,
                    "output": "done b",
                },
            ],
        },
    ]


# OpenAI Chat compatible reasoning


def test_openai_chat_replays_reasoning_by_default() -> None:
    adapter = OpenAIChatAdapter()

    payload_messages = adapter._build_request_payload(
        request_obj(
            max_tokens=2048,
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "think"},
                        {"type": "text", "text": "answer"},
                    ],
                }
            ],
        )
    )["messages"]

    assert payload_messages[0]["reasoning_content"] == "think"


@pytest.mark.parametrize(
    "adapter",
    [OpenAIChatAdapter(), XAIAdapter()],
    ids=["openai_chat", "xai"],
)
@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "high"),
        ("none", "low"),
        (None, None),
    ],
)
def test_openai_chat_clamps_reasoning_effort_to_wire_payload(
    adapter: OpenAIChatAdapter, effort: str | None, expected: str | None
) -> None:
    payload = adapter._build_request_payload(request_obj(model="grok-4.5", reasoning_effort=effort))

    assert payload.get("reasoning_effort") == expected


def test_thinking_duration_metadata_is_not_sent_to_providers() -> None:
    message = {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "text": "think",
                "meta": {
                    "duration_ms": 1234,
                    "native": {
                        "signature": "sig_1",
                        "reasoning_field": "reasoning_content",
                        "part": {"text": "think", "thought": True, "thought_signature": "sig_1"},
                    },
                },
            },
            {"type": "text", "text": "answer"},
        ],
    }
    payloads = [
        AnthropicAdapter()._build_request_payload(request_obj(messages=[message]))["messages"],
        OpenAIChatAdapter()._build_request_payload(request_obj(messages=[message]))["messages"],
        GoogleGeminiAdapter()._build_contents(request_obj(messages=[message])),
        OpenAIResponsesAdapter()._build_request_payload(request_obj(messages=[message]))["input"],
    ]

    for payload in payloads:
        assert "duration_ms" not in json.dumps(payload)


def test_replay_preserves_empty_native_reasoning_blocks() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "text": "", "meta": {"native": {"reasoning_field": "reasoning_content"}}},
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "next question"}]},
    ]

    repaired = repair_messages_for_replay(messages, supports_image_input=True, supports_pdf_input=True)

    assert repaired[0]["content"][0] == {
        "type": "thinking",
        "text": "",
        "meta": {"native": {"reasoning_field": "reasoning_content"}},
    }


def test_deepseek_replays_reasoning_across_turns() -> None:
    adapter = DeepSeekAdapter()

    payload_messages = adapter._build_request_payload(
        request_obj(
            max_tokens=2048,
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
                            "output": "done",
                        }
                    ],
                },
            ],
        )
    )["messages"]
    assert payload_messages[0]["reasoning_content"] == "think"
    payload_messages = adapter._build_request_payload(
        request_obj(
            max_tokens=2048,
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
        )
    )["messages"]
    assert payload_messages[0]["reasoning_content"] == "think"


@pytest.mark.parametrize(
    ("reasoning_field", "thinking_text", "expected_value"),
    [
        ("reasoning_content", "", None),
        ("reasoning", "think", "think"),
    ],
)
def test_openai_chat_replays_native_reasoning_field(
    reasoning_field: str,
    thinking_text: str,
    expected_value: str | None,
) -> None:
    adapter = OpenAIChatAdapter()

    payload_messages = adapter._build_request_payload(
        request_obj(
            max_tokens=2048,
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "text": thinking_text,
                            "meta": {"native": {"reasoning_field": reasoning_field}},
                        },
                        {"type": "text", "text": "done"},
                    ],
                },
                {"role": "user", "content": [{"type": "text", "text": "next question"}]},
            ],
        )
    )["messages"]

    assert payload_messages[0][reasoning_field] == expected_value


def test_deepseek_replays_empty_reasoning_content_after_tool_turn() -> None:
    adapter = DeepSeekAdapter()

    payload_messages = adapter._build_request_payload(
        request_obj(
            max_tokens=2048,
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "text": "Need to run tests.",
                            "meta": {"native": {"reasoning_field": "reasoning_content"}},
                        },
                        {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"command": "pytest"}},
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "ok"}]},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "text": "",
                            "meta": {"native": {"reasoning_field": "reasoning_content"}},
                        },
                        {"type": "text", "text": "All tests passed."},
                    ],
                },
                {"role": "user", "content": [{"type": "text", "text": "continue"}]},
            ],
        )
    )["messages"]

    assert payload_messages[0]["reasoning_content"] == "Need to run tests."
    assert payload_messages[2]["reasoning_content"] is None


# Anthropic-like adapters


def test_anthropic_serializes_image_tool_result_content(tmp_path) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(_PNG_1X1)
    adapter = AnthropicAdapter()

    payload = adapter._build_request_payload(
        request_obj(
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
                            "output": "Read image file [image/png]",
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
        )
    )

    content = payload["messages"][1]["content"][0]["content"]
    assert content[0] == {"type": "text", "text": "Read image file [image/png]"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"


def test_anthropic_replays_thinking_signature_without_tool_use_caller() -> None:
    adapter = AnthropicAdapter()

    payload = adapter._build_request_payload(
        request_obj(
            model="claude-sonnet-4-6",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "think", "meta": {"native": {"signature": "sig_1"}}},
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "read",
                            "input": {},
                            "meta": {"native": {"caller": {"tool_id": "", "type": ""}}},
                        },
                    ],
                    "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                }
            ],
        )
    )["messages"][0]

    assert payload["role"] == "assistant"
    assert payload["content"][0] == {"type": "thinking", "thinking": "think", "signature": "sig_1"}
    assert payload["content"][1] == {"type": "tool_use", "id": "call_1", "name": "read", "input": {}}


def test_anthropic_skips_moonshot_thinking_during_replay() -> None:
    messages = AnthropicAdapter()._build_request_payload(
        request_obj(
            model="claude-sonnet-4-6",
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "Inspect x.py"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need the tool result first."},
                        {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
                    ],
                    "meta": {"provider": "moonshotai", "model": "kimi-k2-thinking"},
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "done"}]},
            ],
        )
    )["messages"]

    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == [{"type": "tool_use", "id": "call_1", "name": "read", "input": {}}]
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert messages[2]["content"][0]["tool_use_id"] == "call_1"


def test_moonshot_replays_unsigned_thinking_without_signature() -> None:
    payload = MoonshotAIAdapter()._build_request_payload(
        request_obj(
            model="kimi-k2-thinking",
            messages=[
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "text": "Need the tool result first."},
                        {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
                    ],
                }
            ],
        )
    )["messages"][0]

    assert payload["role"] == "assistant"
    assert payload["content"][0] == {"type": "thinking", "thinking": "Need the tool result first."}
    assert payload["content"][1]["type"] == "tool_use"
    assert payload["content"][1]["id"] == "call_1"


@pytest.mark.parametrize(
    "adapter",
    [
        pytest.param(AnthropicAdapter(), id="anthropic"),
        pytest.param(MoonshotAIAdapter(), id="moonshotai"),
        pytest.param(MiniMaxAdapter(), id="minimax"),
    ],
)
def test_anthropic_like_build_request_payload_adds_cache_control(adapter) -> None:
    request = request_obj(
        max_tokens=4096,
        system="You are helpful.",
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
                        "output": "tool output",
                        "is_error": False,
                    },
                ],
            },
        ],
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
