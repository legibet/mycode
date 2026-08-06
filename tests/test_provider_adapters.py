from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic import APIStatusError as AnthropicAPIStatusError

from mycode.compact import build_compact_event
from mycode.providers import (
    AlibabaAdapter,
    AnthropicAdapter,
    DeepSeekAdapter,
    GoogleGeminiAdapter,
    MiniMaxAdapter,
    MoonshotAIAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    OpenRouterAdapter,
    XAIAdapter,
    ZAIAdapter,
)
from mycode.providers.base import ProviderError, ProviderRequest, repair_messages_for_replay
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


def request_obj(**overrides: Any) -> ProviderRequest:
    request = ProviderRequest(
        provider="test",
        model="test-model",
        session_id=None,
        messages=[],
        system="",
        tools=[],
        max_tokens=4096,
        temperature=1.0,
        api_key=None,
        api_base=None,
    )
    return replace(request, **overrides)


def _async_context_mock() -> MagicMock:
    mock = MagicMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


def _stream_mock(items: list[Any], *, final_message: Any = None) -> MagicMock:
    stream = _async_context_mock()
    stream.__aiter__.return_value = iter(items)
    if final_message is not None:
        stream.get_final_message = AsyncMock(return_value=final_message)
    return stream


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


# Request replay and media


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


async def test_openai_responses_replays_foreign_thinking_as_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = OpenAIResponsesAdapter()
    client = _async_context_mock()
    client.responses.create = AsyncMock(
        return_value=_stream_mock(
            [
                _Obj(
                    type="response.completed",
                    response=_Obj(
                        id="resp_1",
                        model="gpt-5.4",
                        status="completed",
                        usage=_Obj(input_tokens=20, output_tokens=2, total_tokens=22),
                        output=[],
                    ),
                )
            ]
        )
    )
    monkeypatch.setattr("mycode.providers.openai_responses.AsyncOpenAI", lambda **_kwargs: client)

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="gpt-5.4",
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
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "42"}],
                    },
                ],
            )
        )
    ]

    request_call = client.responses.create.await_args
    assert request_call is not None
    input_items = request_call.kwargs["input"]
    assert [item["type"] for item in input_items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert input_items[1]["role"] == "assistant"
    assert input_items[1]["content"] == [
        {"type": "output_text", "text": "Need the tool first.\nI will inspect the file."}
    ]
    assert input_items[2]["call_id"] == "call_1"
    assert input_items[3]["output"] == "42"


@pytest.mark.parametrize(
    ("code", "retryable"),
    [("server_error", True), ("invalid_prompt", False)],
)
async def test_openai_responses_classifies_stream_failures(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    retryable: bool,
) -> None:
    client = _async_context_mock()
    client.responses.create = AsyncMock(
        return_value=_stream_mock(
            [
                _Obj(
                    type="response.failed",
                    response=_Obj(error=_Obj(code=code, message="request failed")),
                )
            ]
        )
    )
    monkeypatch.setattr("mycode.providers.openai_responses.AsyncOpenAI", lambda **_kwargs: client)

    with pytest.raises(ProviderError) as caught:
        async for _ in OpenAIResponsesAdapter().stream_turn(request_obj(api_key="test-key")):
            pass

    assert caught.value.retryable is retryable


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


# Response conversion


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


# Strict tool schemas


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


async def test_google_gemini_replays_foreign_thinking_as_plain_model_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = GoogleGeminiAdapter()
    client = MagicMock()
    client.aio.models.generate_content_stream = AsyncMock(return_value=_stream_mock([]))
    client.aio.aclose = AsyncMock()
    monkeypatch.setattr("mycode.providers.gemini.genai.Client", lambda **_kwargs: client)

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
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
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "42"}],
                    },
                ],
            )
        )
    ]

    request_call = client.aio.models.generate_content_stream.await_args
    assert request_call is not None
    contents = request_call.kwargs["contents"]
    assert [content["role"] for content in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][0] == {"text": "Need the tool first."}
    assert contents[1]["parts"][2]["function_call"] == {"id": "call_1", "name": "read", "args": {"path": "x.py"}}
    assert contents[1]["parts"][2]["thought_signature"] == "skip_thought_signature_validator"
    assert contents[2]["parts"][0]["function_response"]["response"] == {"result": "42"}


async def test_google_gemini_replaces_native_signatures_after_model_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.aio.models.generate_content_stream = AsyncMock(return_value=_stream_mock([]))
    client.aio.aclose = AsyncMock()
    monkeypatch.setattr("mycode.providers.gemini.genai.Client", lambda **_kwargs: client)

    _ = [
        event
        async for event in GoogleGeminiAdapter().stream_turn(
            request_obj(
                api_key="test-key",
                model="gemini-3.6-flash",
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "text": "Need the tool result first.",
                                "meta": {
                                    "native": {
                                        "part": {
                                            "text": "Need the tool result first.",
                                            "thought": True,
                                            "thought_signature": "old-thinking-signature",
                                        }
                                    }
                                },
                            },
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "read",
                                "input": {},
                                "meta": {
                                    "native": {
                                        "part": {
                                            "function_call": {"id": "call_1", "name": "read", "args": {}},
                                            "thought_signature": "old-tool-signature",
                                        }
                                    }
                                },
                            },
                        ],
                        "meta": {"provider": "google", "model": "gemini-3.1-pro-preview"},
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "done"}],
                    },
                ],
            )
        )
    ]

    request_call = client.aio.models.generate_content_stream.await_args
    assert request_call is not None
    parts = request_call.kwargs["contents"][0]["parts"]
    assert parts[0] == {"text": "Need the tool result first."}
    assert parts[1]["thought_signature"] == "skip_thought_signature_validator"


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


@pytest.mark.parametrize(
    "adapter",
    [
        pytest.param(OpenAIChatAdapter(), id="openai-chat"),
        pytest.param(DeepSeekAdapter(), id="deepseek"),
        pytest.param(ZAIAdapter(), id="zai"),
        pytest.param(XAIAdapter(), id="xai"),
    ],
)
async def test_chat_targets_replay_foreign_thinking_as_assistant_content(
    monkeypatch: pytest.MonkeyPatch,
    adapter: OpenAIChatAdapter,
) -> None:
    client = _async_context_mock()
    client.chat.completions.create = AsyncMock(return_value=_stream_mock([]))
    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", lambda **_kwargs: client)

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="target-model",
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "text": "Need the tool first."},
                            {"type": "text", "text": "I will inspect the file."},
                        ],
                        "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                    }
                ],
            )
        )
    ]

    request_call = client.chat.completions.create.await_args
    assert request_call is not None
    replayed = request_call.kwargs["messages"][0]
    assert replayed["content"] == "Need the tool first.\nI will inspect the file."
    assert not {"reasoning", "reasoning_content", "reasoning_details"} & replayed.keys()


async def test_openrouter_replays_foreign_thinking_through_reasoning_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = OpenRouterAdapter()
    client = _async_context_mock()
    client.chat.completions.create = AsyncMock(return_value=_stream_mock([]))
    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", lambda **_kwargs: client)

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="openai/gpt-5.6",
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "text": "Need the tool first."},
                            {"type": "text", "text": "I will inspect the file."},
                        ],
                        "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                    }
                ],
            )
        )
    ]

    request_call = client.chat.completions.create.await_args
    assert request_call is not None
    replayed = request_call.kwargs["messages"][0]
    assert replayed["reasoning"] == "Need the tool first."
    assert replayed["content"] == "I will inspect the file."


async def test_openrouter_preserves_structured_reasoning_across_models(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = OpenRouterAdapter()
    reasoning_details = [
        {
            "type": "reasoning.text",
            "text": "first",
            "signature": "signature",
            "id": "reasoning-1",
            "format": "anthropic-claude-v1",
            "index": 0,
        },
        {
            "type": "reasoning.encrypted",
            "data": "encrypted",
            "id": "reasoning-2",
            "format": "anthropic-claude-v1",
            "index": 1,
        },
    ]

    chunks = [
        _Obj(
            id="response-1",
            model="anthropic/claude-sonnet-4.6",
            usage=None,
            choices=[
                _Obj(
                    finish_reason=None,
                    delta=_Obj(
                        reasoning="first",
                        reasoning_details=[reasoning_details[0]],
                        content=None,
                        tool_calls=[],
                    ),
                )
            ],
        ),
        _Obj(
            id="response-1",
            model="anthropic/claude-sonnet-4.6",
            usage=None,
            choices=[
                _Obj(
                    finish_reason="stop",
                    delta=_Obj(
                        reasoning=" second",
                        reasoning_details=[reasoning_details[1]],
                        content="answer",
                        tool_calls=[],
                    ),
                )
            ],
        ),
    ]
    clients = []

    def fake_client(**_kwargs: Any) -> MagicMock:
        client = _async_context_mock()
        stream = _stream_mock(chunks if not clients else [])
        client.chat.completions.create = AsyncMock(return_value=stream)
        clients.append(client)
        return client

    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", fake_client)

    first_events = [
        event
        async for event in adapter.stream_turn(request_obj(api_key="test-key", model="anthropic/claude-sonnet-4.6"))
    ]
    stored_message = first_events[-1].data["message"]
    assert stored_message["content"][0]["text"] == "first second"

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="openai/gpt-5.6",
                messages=[stored_message, {"role": "user", "content": [{"type": "text", "text": "continue"}]}],
            )
        )
    ]
    replayed_messages = clients[1].chat.completions.create.await_args.kwargs["messages"]
    assert replayed_messages[0]["reasoning_details"] == reasoning_details


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        pytest.param(OpenAIChatAdapter(), {"reasoning_effort": "vendor-specific"}, id="openai-chat"),
        pytest.param(XAIAdapter(), {"reasoning_effort": "vendor-specific"}, id="xai"),
        pytest.param(
            DeepSeekAdapter(),
            {
                "reasoning_effort": "vendor-specific",
                "extra_body": {"thinking": {"type": "enabled"}},
            },
            id="deepseek",
        ),
        pytest.param(
            ZAIAdapter(),
            {
                "reasoning_effort": "vendor-specific",
                "extra_body": {"thinking": {"type": "enabled", "clear_thinking": False}},
            },
            id="zai",
        ),
        pytest.param(
            OpenRouterAdapter(),
            {"extra_body": {"reasoning": {"effort": "vendor-specific"}}},
            id="openrouter",
        ),
    ],
)
def test_chat_adapters_forward_reasoning_effort_in_their_wire_format(
    adapter: OpenAIChatAdapter, expected: dict[str, Any]
) -> None:
    payload = adapter._build_request_payload(request_obj(model="unlisted-model", reasoning_effort="vendor-specific"))

    assert {key: payload[key] for key in expected} == expected


def test_deepseek_none_disables_thinking() -> None:
    payload = DeepSeekAdapter()._build_request_payload(request_obj(reasoning_effort="none"))

    assert payload["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in payload


def test_alibaba_builds_provider_specific_payload() -> None:
    payload = AlibabaAdapter()._build_request_payload(request_obj(reasoning_effort="xhigh"))

    assert payload["extra_body"] == {"preserve_thinking": True}
    assert payload["reasoning_effort"] == "xhigh"
    assert payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in payload


@pytest.mark.parametrize(
    ("adapter", "expected_thinking"),
    [
        pytest.param(
            AnthropicAdapter(),
            {"type": "adaptive", "display": "summarized"},
            id="anthropic",
        ),
        pytest.param(MoonshotAIAdapter(), {"type": "adaptive"}, id="moonshot"),
    ],
)
def test_anthropic_like_adapters_forward_reasoning_effort(
    adapter: AnthropicAdapter | MoonshotAIAdapter, expected_thinking: dict[str, Any]
) -> None:
    payload = adapter._build_request_payload(request_obj(model="unlisted-model", reasoning_effort="vendor-specific"))

    assert payload["thinking"] == expected_thinking
    assert payload["output_config"] == {"effort": "vendor-specific"}


@pytest.mark.parametrize(
    "adapter",
    [AnthropicAdapter(), MoonshotAIAdapter()],
    ids=["anthropic", "moonshot"],
)
def test_anthropic_like_none_disables_thinking(adapter: AnthropicAdapter | MoonshotAIAdapter) -> None:
    payload = adapter._build_request_payload(request_obj(reasoning_effort="none"))

    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload


def test_gemini_projects_reasoning_effort_as_thinking_level() -> None:
    config = GoogleGeminiAdapter()._build_config(request_obj(model="unlisted-model", reasoning_effort="minimal"))

    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level is not None
    assert config.thinking_config.thinking_level.value == "MINIMAL"


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


@pytest.mark.parametrize(
    ("reasoning_field", "thinking_text", "expected_value"),
    [
        ("reasoning_content", "", None),
        ("reasoning", "think", "think"),
    ],
)
async def test_openai_chat_replays_its_native_reasoning_field(
    monkeypatch: pytest.MonkeyPatch,
    reasoning_field: str,
    thinking_text: str,
    expected_value: str | None,
) -> None:
    adapter = OpenAIChatAdapter()
    clients = []

    def fake_client(**_kwargs: Any) -> MagicMock:
        client = _async_context_mock()
        delta = _Obj(content="done", tool_calls=[], **{reasoning_field: thinking_text})
        chunks = [
            _Obj(
                id="response-1",
                model="resolved-model-version",
                usage=None,
                choices=[_Obj(finish_reason="stop", delta=delta)],
            )
        ]
        client.chat.completions.create = AsyncMock(return_value=_stream_mock(chunks if not clients else []))
        clients.append(client)
        return client

    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", fake_client)

    first_events = [event async for event in adapter.stream_turn(request_obj(api_key="test-key", model="test-model"))]
    stored_message = first_events[-1].data["message"]

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="test-model",
                messages=[stored_message, {"role": "user", "content": [{"type": "text", "text": "continue"}]}],
            )
        )
    ]

    replayed = clients[1].chat.completions.create.await_args.kwargs["messages"][0]
    assert replayed[reasoning_field] == expected_value


@pytest.mark.parametrize(
    ("thinking_text", "expected_value"),
    [
        pytest.param("Need to run tests.", "Need to run tests.", id="text"),
        pytest.param("", None, id="empty-marker"),
    ],
)
async def test_deepseek_replays_native_reasoning_across_turns(
    monkeypatch: pytest.MonkeyPatch,
    thinking_text: str,
    expected_value: str | None,
) -> None:
    adapter = DeepSeekAdapter()
    clients = []

    def fake_client(**_kwargs: Any) -> MagicMock:
        client = _async_context_mock()
        chunks = [
            _Obj(
                id="response-1",
                model="deepseek-v4-pro",
                usage=None,
                choices=[
                    _Obj(
                        finish_reason="stop",
                        delta=_Obj(
                            reasoning_content=thinking_text,
                            content="All tests passed.",
                            tool_calls=[],
                        ),
                    )
                ],
            )
        ]
        client.chat.completions.create = AsyncMock(return_value=_stream_mock(chunks if not clients else []))
        clients.append(client)
        return client

    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", fake_client)

    first_events = [
        event async for event in adapter.stream_turn(request_obj(api_key="test-key", model="deepseek-v4-pro"))
    ]
    stored_message = first_events[-1].data["message"]

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="deepseek-v4-pro",
                messages=[stored_message, {"role": "user", "content": [{"type": "text", "text": "continue"}]}],
            )
        )
    ]

    replayed = clients[1].chat.completions.create.await_args.kwargs["messages"][0]
    assert replayed["reasoning_content"] == expected_value


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


async def test_anthropic_preserves_native_thinking_blocks_across_tool_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AnthropicAdapter()
    first_response = _Obj(
        id="msg_1",
        model="claude-sonnet-5-20260701",
        stop_reason="tool_use",
        stop_sequence=None,
        service_tier=None,
        usage=_Obj(input_tokens=10, output_tokens=5),
        content=[
            _Obj(type="thinking", thinking="Think", signature="signature"),
            _Obj(type="redacted_thinking", data="encrypted"),
            _Obj(type="tool_use", id="call_1", name="read", input={}),
        ],
    )
    second_response = _Obj(
        id="msg_2",
        model="claude-sonnet-5-20260701",
        stop_reason="end_turn",
        stop_sequence=None,
        service_tier=None,
        usage=_Obj(input_tokens=20, output_tokens=3),
        content=[_Obj(type="text", text="done", citations=[])],
    )

    client = _async_context_mock()
    streams = [_stream_mock([], final_message=response) for response in (first_response, second_response)]
    client.messages.stream.side_effect = streams
    monkeypatch.setattr("mycode.providers.anthropic_like.AsyncAnthropic", lambda **_kwargs: client)

    first_events = [
        event async for event in adapter.stream_turn(request_obj(api_key="test-key", model="claude-sonnet-5"))
    ]
    stored_message = first_events[-1].data["message"]
    assert stored_message["content"][0]["text"] == "Think"

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="claude-sonnet-5",
                messages=[
                    stored_message,
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "done"}],
                    },
                ],
            )
        )
    ]
    replayed_request = client.messages.stream.call_args_list[1].kwargs
    assert replayed_request["messages"][0]["content"][:2] == [
        {"type": "thinking", "thinking": "Think", "signature": "signature"},
        {"type": "redacted_thinking", "data": "encrypted"},
    ]
    assert replayed_request["thinking"]["display"] == "summarized"


@pytest.mark.parametrize(
    ("error_type", "retryable"),
    [("overloaded_error", True), ("invalid_request_error", False)],
)
async def test_anthropic_classifies_stream_failures(
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
    retryable: bool,
) -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(200, request=request)
    sdk_error = AnthropicAPIStatusError(
        "stream failed",
        response=response,
        body={"error": {"type": error_type}},
    )
    client = _async_context_mock()
    client.messages.stream.side_effect = sdk_error
    monkeypatch.setattr("mycode.providers.anthropic_like.AsyncAnthropic", lambda **_kwargs: client)

    with pytest.raises(ProviderError) as caught:
        async for _ in AnthropicAdapter().stream_turn(request_obj(api_key="test-key")):
            pass

    assert caught.value.retryable is retryable
    assert caught.value.status_code is None


@pytest.mark.parametrize(
    ("adapter", "model"),
    [
        pytest.param(AnthropicAdapter(), "claude-sonnet-4-6", id="anthropic"),
        pytest.param(MoonshotAIAdapter(), "kimi-k2.7-code", id="moonshotai"),
        pytest.param(MiniMaxAdapter(), "MiniMax-M3", id="minimax"),
    ],
)
async def test_anthropic_like_targets_replay_foreign_thinking_as_assistant_text(
    monkeypatch: pytest.MonkeyPatch,
    adapter: Any,
    model: str,
) -> None:
    client = _async_context_mock()
    client.messages.stream.return_value = _stream_mock(
        [],
        final_message=_Obj(
            id="msg_2",
            model=model,
            stop_reason="end_turn",
            stop_sequence=None,
            service_tier=None,
            usage=_Obj(input_tokens=20, output_tokens=3),
            content=[_Obj(type="text", text="done", citations=[])],
        ),
    )
    monkeypatch.setattr("mycode.providers.anthropic_like.AsyncAnthropic", lambda **_kwargs: client)

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model=model,
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": "Inspect x.py"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "text": "Need the tool result first."},
                            {"type": "text", "text": "I will inspect the file."},
                            {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
                        ],
                        "meta": {"provider": "openai", "model": "gpt-5.6"},
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "done"}],
                    },
                ],
            )
        )
    ]

    replayed = client.messages.stream.call_args.kwargs["messages"][1]
    assert replayed["content"][:2] == [
        {"type": "text", "text": "Need the tool result first."},
        {"type": "text", "text": "I will inspect the file."},
    ]
    assert replayed["content"][2]["type"] == "tool_use"


async def test_anthropic_does_not_reuse_thinking_signature_after_model_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _async_context_mock()
    client.messages.stream.return_value = _stream_mock(
        [],
        final_message=_Obj(
            id="msg_2",
            model="claude-sonnet-5",
            stop_reason="end_turn",
            stop_sequence=None,
            service_tier=None,
            usage=_Obj(input_tokens=20, output_tokens=3),
            content=[_Obj(type="text", text="done", citations=[])],
        ),
    )
    monkeypatch.setattr("mycode.providers.anthropic_like.AsyncAnthropic", lambda **_kwargs: client)

    _ = [
        event
        async for event in AnthropicAdapter().stream_turn(
            request_obj(
                api_key="test-key",
                model="claude-sonnet-5",
                messages=[
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "text": "Need the tool result first.",
                                "meta": {
                                    "native": {
                                        "anthropic_block": {
                                            "type": "thinking",
                                            "thinking": "Need the tool result first.",
                                            "signature": "old-signature",
                                        }
                                    }
                                },
                            },
                            {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
                        ],
                        "meta": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "done"}],
                    },
                ],
            )
        )
    ]

    replayed = client.messages.stream.call_args.kwargs["messages"][0]
    assert replayed["content"][0] == {"type": "text", "text": "Need the tool result first."}


async def test_moonshot_replays_native_unsigned_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = MoonshotAIAdapter()
    first_response = _Obj(
        id="msg_1",
        model="kimi-k2.7-code",
        stop_reason="tool_use",
        stop_sequence=None,
        service_tier=None,
        usage=_Obj(input_tokens=10, output_tokens=5),
        content=[
            _Obj(type="thinking", thinking="Need the tool result first."),
            _Obj(type="tool_use", id="call_1", name="read", input={}),
        ],
    )
    second_response = _Obj(
        id="msg_2",
        model="kimi-k2.7-code",
        stop_reason="end_turn",
        stop_sequence=None,
        service_tier=None,
        usage=_Obj(input_tokens=20, output_tokens=3),
        content=[_Obj(type="text", text="done", citations=[])],
    )

    client = _async_context_mock()
    client.messages.stream.side_effect = [
        _stream_mock([], final_message=response) for response in (first_response, second_response)
    ]
    monkeypatch.setattr("mycode.providers.anthropic_like.AsyncAnthropic", lambda **_kwargs: client)

    first_events = [
        event async for event in adapter.stream_turn(request_obj(api_key="test-key", model="kimi-k2.7-code"))
    ]
    stored_message = first_events[-1].data["message"]

    _ = [
        event
        async for event in adapter.stream_turn(
            request_obj(
                api_key="test-key",
                model="kimi-k2.7-code",
                messages=[
                    stored_message,
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "call_1", "output": "done"}],
                    },
                ],
            )
        )
    ]

    replayed = client.messages.stream.call_args_list[1].kwargs["messages"][0]
    assert replayed["content"][0] == {"type": "thinking", "thinking": "Need the tool result first."}
    assert replayed["content"][1]["type"] == "tool_use"


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


# Usage normalization


def test_anthropic_normalizes_usage_details() -> None:
    message = _Obj(
        id="msg_1",
        model="claude-sonnet-4-5",
        stop_reason="end_turn",
        content=[_Obj(type="text", text="hi", citations=None)],
        usage=_Obj(
            input_tokens=1_000,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=10_000,
            output_tokens=900,
            output_tokens_details=_Obj(thinking_tokens=600),
        ),
    )

    converted = AnthropicAdapter()._convert_final_message(message)

    assert converted["meta"]["usage"] == {
        "total_tokens": 12_100,
        "input_tokens": 11_200,
        "cache_read_tokens": 10_000,
        "cache_write_tokens": 200,
        "output_tokens": 900,
        "reasoning_tokens": 600,
    }
    assert converted["meta"]["native"]["usage"]["cache_read_input_tokens"] == 10_000


def test_anthropic_missing_cache_fields_leave_input_unknown() -> None:
    # Unverified compatible upstream reporting only the base counters: the
    # effective input must stay unknown rather than silently understated.
    message = _Obj(
        id="msg_1",
        model="kimi-latest",
        stop_reason="end_turn",
        content=[],
        usage=_Obj(input_tokens=100, output_tokens=5),
    )

    converted = MoonshotAIAdapter()._convert_final_message(message)

    assert converted["meta"]["usage"] == {"output_tokens": 5}


def test_openai_responses_normalizes_usage_details() -> None:
    response = _Obj(
        id="resp_1",
        model="gpt-5.4",
        status="completed",
        usage=_Obj(
            input_tokens=1_000,
            input_tokens_details=_Obj(cached_tokens=800, cache_write_tokens=100),
            output_tokens=50,
            output_tokens_details=_Obj(reasoning_tokens=30),
            total_tokens=1_050,
        ),
        output=[],
    )

    converted = OpenAIResponsesAdapter()._convert_final_response(response)

    assert converted["meta"]["usage"] == {
        "total_tokens": 1_050,
        "input_tokens": 1_000,
        "cache_read_tokens": 800,
        "cache_write_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 30,
    }
    assert converted["meta"]["native"]["usage"]["input_tokens_details"] == {
        "cached_tokens": 800,
        "cache_write_tokens": 100,
    }


async def test_openai_chat_normalizes_usage_details(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _async_context_mock()
    chunks = [
        _Obj(
            id="response-1",
            usage=_Obj(
                prompt_tokens=1_000,
                prompt_tokens_details=_Obj(cached_tokens=700),
                completion_tokens=80,
                completion_tokens_details=_Obj(reasoning_tokens=60),
                total_tokens=1_080,
                # A non-OpenRouter `cost` extension has unknown semantics and
                # must not surface as cost_usd (the exact match below proves it).
                cost=0.0123,
            ),
            choices=[_Obj(finish_reason="stop", delta=_Obj(content="done", tool_calls=[]))],
        )
    ]
    client.chat.completions.create = AsyncMock(return_value=_stream_mock(chunks))
    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", lambda **_kwargs: client)

    events = [event async for event in OpenAIChatAdapter().stream_turn(request_obj(api_key="k", model="m"))]

    assert events[-1].data["message"]["meta"]["usage"] == {
        "total_tokens": 1_080,
        "input_tokens": 1_000,
        "cache_read_tokens": 700,
        "output_tokens": 80,
        "reasoning_tokens": 60,
    }


async def test_deepseek_falls_back_to_its_cache_hit_extension_field(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _async_context_mock()
    chunks = [
        _Obj(
            id="response-1",
            usage=_Obj(
                prompt_tokens=100,
                prompt_cache_hit_tokens=80,
                prompt_cache_miss_tokens=20,
                completion_tokens=10,
                total_tokens=110,
            ),
            choices=[_Obj(finish_reason="stop", delta=_Obj(content="done", tool_calls=[]))],
        )
    ]
    client.chat.completions.create = AsyncMock(return_value=_stream_mock(chunks))
    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", lambda **_kwargs: client)

    events = [event async for event in DeepSeekAdapter().stream_turn(request_obj(api_key="k", model="deepseek-chat"))]

    usage = events[-1].data["message"]["meta"]["usage"]
    assert usage["cache_read_tokens"] == 80
    assert usage["input_tokens"] == 100


async def test_openrouter_stores_the_charged_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _async_context_mock()
    chunks = [
        _Obj(
            id="response-1",
            usage=_Obj(prompt_tokens=100, completion_tokens=10, total_tokens=110, cost=0.0123),
            choices=[_Obj(finish_reason="stop", delta=_Obj(content="done", tool_calls=[]))],
        )
    ]
    client.chat.completions.create = AsyncMock(return_value=_stream_mock(chunks))
    monkeypatch.setattr("mycode.providers.openai_chat.AsyncOpenAI", lambda **_kwargs: client)

    events = [event async for event in OpenRouterAdapter().stream_turn(request_obj(api_key="k", model="vendor/model"))]

    # Usage accounting is on by default at OpenRouter; no opt-in is sent.
    request_call = client.chat.completions.create.await_args
    assert request_call is not None
    assert "extra_body" not in request_call.kwargs
    assert events[-1].data["message"]["meta"]["usage"]["cost_usd"] == 0.0123


async def test_gemini_normalizes_usage_details(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    chunks = [
        _Obj(
            response_id="resp-1",
            usage_metadata=_Obj(
                prompt_token_count=100,
                cached_content_token_count=40,
                candidates_token_count=20,
                thoughts_token_count=15,
                total_token_count=135,
            ),
            candidates=[],
        )
    ]
    client.aio.models.generate_content_stream = AsyncMock(return_value=_stream_mock(chunks))
    client.aio.aclose = AsyncMock()
    monkeypatch.setattr("mycode.providers.gemini.genai.Client", lambda **_kwargs: client)

    events = [
        event async for event in GoogleGeminiAdapter().stream_turn(request_obj(api_key="k", model="gemini-3.6-flash"))
    ]

    # Output includes thoughts; the absent tool-use prompt count means zero.
    assert events[-1].data["message"]["meta"]["usage"] == {
        "total_tokens": 135,
        "input_tokens": 100,
        "cache_read_tokens": 40,
        "output_tokens": 35,
        "reasoning_tokens": 15,
    }
