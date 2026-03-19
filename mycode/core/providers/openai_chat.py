"""OpenAI Chat Completions adapter built on the official OpenAI SDK.

This adapter exists for providers that expose an OpenAI-compatible
`/chat/completions` interface. It is also useful for third-party providers that
do not implement the Responses API yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openai import APIError, AsyncOpenAI

from mycode.core.messages import (
    assistant_message,
    text_block,
    thinking_block,
    tool_use_block,
)
from mycode.core.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
)
from mycode.core.tools import parse_tool_arguments


@dataclass
class _ChatToolCallState:
    index: int
    tool_id: str | None = None
    name: str = ""
    arguments_parts: list[str] = field(default_factory=list)


class OpenAIChatAdapter(ProviderAdapter):
    provider_id = "openai_chat"
    label = "OpenAI Chat Completions"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)

    async def stream_turn(self, request: ProviderRequest):
        api_key = self.require_api_key(request.api_key)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.resolve_base_url(request.api_base),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

        tool_calls: dict[int, _ChatToolCallState] = {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_meta: dict[str, Any] = {}
        response_id: str | None = None
        response_model: str | None = None
        finish_reason: str | None = None
        usage: Any = None

        try:
            stream = await client.chat.completions.create(**self._build_request_payload(request), stream=True)
            async for chunk in stream:
                response_id = response_id or getattr(chunk, "id", None)
                response_model = response_model or getattr(chunk, "model", None)

                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = choice.delta
                reasoning_delta, reasoning_meta_update = self._extract_reasoning_delta(delta)
                if reasoning_delta:
                    thinking_parts.append(reasoning_delta)
                    thinking_meta.update(reasoning_meta_update)
                    yield ProviderStreamEvent("thinking_delta", {"text": reasoning_delta})

                if delta.content:
                    text_parts.append(delta.content)
                    yield ProviderStreamEvent("text_delta", {"text": delta.content})

                for tool_call in delta.tool_calls or []:
                    state = tool_calls.setdefault(tool_call.index or 0, _ChatToolCallState(index=tool_call.index or 0))
                    if tool_call.id:
                        state.tool_id = tool_call.id
                    function = tool_call.function
                    if function is None:
                        continue
                    if function.name:
                        state.name = function.name
                    if function.arguments:
                        state.arguments_parts.append(function.arguments)
        except APIError as exc:
            raise ValueError(str(exc)) from exc

        blocks = []
        if thinking_parts:
            blocks.append(thinking_block("".join(thinking_parts), meta=thinking_meta or None))
        if text_parts:
            blocks.append(text_block("".join(text_parts)))

        for index in sorted(tool_calls):
            state = tool_calls[index]
            raw_arguments = "".join(state.arguments_parts)
            parsed_arguments = parse_tool_arguments(raw_arguments)
            if isinstance(parsed_arguments, str):
                tool_input = {}
                meta = {"raw_arguments": raw_arguments}
            else:
                tool_input = parsed_arguments
                meta = None

            blocks.append(
                tool_use_block(
                    tool_id=state.tool_id or f"tool_call_{index}",
                    name=state.name,
                    input=tool_input,
                    meta=meta,
                )
            )

        final_message = assistant_message(
            blocks,
            provider=self.provider_id,
            model=response_model or request.model,
            provider_message_id=response_id,
            stop_reason=finish_reason,
            usage=_dump_model(usage),
        )
        yield ProviderStreamEvent("message_done", {"message": final_message})

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            messages.extend(self._serialize_message(message))

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "tools": [self._serialize_tool(tool) for tool in request.tools] or None,
            "tool_choice": "auto" if request.tools else None,
            "max_tokens": request.max_tokens,
            "stream_options": {"include_usage": True},
        }
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        return {key: value for key, value in payload.items() if value is not None}

    def _serialize_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }

    def _serialize_message(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        role = str(message.get("role") or "user")
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]

        if role == "user":
            tool_result_messages = [
                self._serialize_tool_result(block) for block in blocks if block.get("type") == "tool_result"
            ]
            text_parts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
            messages = []
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(part for part in text_parts if part)})
            messages.extend(tool_result_messages)
            return messages

        if role != "assistant":
            return []

        text_parts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
        thinking_blocks = [block for block in blocks if block.get("type") == "thinking"]
        tool_use_blocks = [block for block in blocks if block.get("type") == "tool_use"]

        payload: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(part for part in text_parts if part),
        }

        if tool_use_blocks:
            payload["tool_calls"] = [self._serialize_tool_use(block) for block in tool_use_blocks]

        if thinking_blocks:
            thinking_text = "\n".join(str(block.get("text") or "") for block in thinking_blocks if block.get("text"))
            raw_meta = thinking_blocks[0].get("meta")
            meta: dict[str, Any] = {}
            if isinstance(raw_meta, dict):
                meta = dict(raw_meta)
            reasoning_field = str(meta.get("openai_reasoning_field") or "")
            if reasoning_field == "reasoning_content":
                payload["reasoning_content"] = thinking_text
            elif reasoning_field == "reasoning_details":
                payload["reasoning_details"] = meta.get("reasoning_details") or []

        return [payload]

    def _serialize_tool_result(self, block: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": block.get("tool_use_id") or "",
            "content": str(block.get("content") or ""),
        }

    def _serialize_tool_use(self, block: dict[str, Any]) -> dict[str, Any]:
        raw_input = block.get("input")
        tool_input: dict[str, Any] = {}
        if isinstance(raw_input, dict):
            tool_input = dict(raw_input)
        return {
            "id": block.get("id") or "",
            "type": "function",
            "function": {
                "name": block.get("name") or "",
                "arguments": _dump_json(tool_input),
            },
        }

    def _extract_reasoning_delta(self, delta: Any) -> tuple[str, dict[str, Any]]:
        extras = getattr(delta, "model_extra", None) or {}
        reasoning_content = extras.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            return reasoning_content, {"openai_reasoning_field": "reasoning_content"}

        reasoning_details = extras.get("reasoning_details")
        if isinstance(reasoning_details, list) and reasoning_details:
            text = "".join(str(item.get("text") or "") for item in reasoning_details if isinstance(item, dict))
            if text:
                return text, {
                    "openai_reasoning_field": "reasoning_details",
                    "reasoning_details": reasoning_details,
                }

        return "", {}


def _dump_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _dump_model(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value
