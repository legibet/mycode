from __future__ import annotations

from mycode.core.providers.openai_chat import OpenAIChatAdapter
from mycode.core.providers.openai_responses import OpenAIResponsesAdapter


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def model_dump(self):
        return dict(self.__dict__)


def test_openai_responses_builds_initial_input_items() -> None:
    adapter = OpenAIResponsesAdapter()
    request = _Obj(
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello"}],
            }
        ]
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
    request = _Obj(
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
    )

    input_items, previous_response_id = adapter._build_input_items(request)

    assert previous_response_id == "resp_123"
    assert input_items == [{"type": "function_call_output", "call_id": "call_1", "output": "file contents"}]


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


def test_openai_chat_extracts_reasoning_from_known_extra_fields() -> None:
    adapter = OpenAIChatAdapter()

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
