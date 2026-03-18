"""OpenAI Responses adapter built on the official OpenAI SDK."""

from __future__ import annotations

from typing import Any

from openai import APIError, AsyncOpenAI

from mycode.core.messages import build_message, text_block, thinking_block, tool_use_block
from mycode.core.providers.base import DEFAULT_REQUEST_TIMEOUT, ProviderAdapter, ProviderRequest, ProviderStreamEvent
from mycode.core.tools import parse_tool_arguments


class OpenAIResponsesAdapter(ProviderAdapter):
    provider_id = "openai"
    label = "OpenAI Responses"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)

    async def stream_turn(self, request: ProviderRequest):
        api_key = self.require_api_key(request.api_key)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.resolve_base_url(request.api_base),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

        payload = self._build_request_payload(request)
        try:
            stream = await client.responses.create(**payload, stream=True)
            final_response = None
            async for event in stream:
                if event.type == "response.reasoning_text.delta" and event.delta:
                    yield ProviderStreamEvent("thinking_delta", {"text": event.delta})
                    continue

                if event.type == "response.output_text.delta" and event.delta:
                    yield ProviderStreamEvent("text_delta", {"text": event.delta})
                    continue

                if event.type == "response.error":
                    raise ValueError(str(getattr(event, "error", None) or event))

                if event.type == "response.failed":
                    raise ValueError(str(getattr(event, "response", None) or event))

                if event.type == "response.completed":
                    final_response = event.response
        except APIError as exc:
            raise ValueError(str(exc)) from exc

        if final_response is None:
            raise ValueError("OpenAI Responses stream ended before response.completed")

        yield ProviderStreamEvent("message_done", {"message": self._convert_final_response(final_response)})

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        input_items, previous_response_id = self._build_input_items(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "instructions": request.system or None,
            "previous_response_id": previous_response_id,
            "max_output_tokens": request.max_tokens,
            "tools": [self._serialize_tool(tool) for tool in request.tools] or None,
            "tool_choice": "auto" if request.tools else None,
        }
        if request.reasoning_effort:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        return {key: value for key, value in payload.items() if value is not None}

    def _build_input_items(self, request: ProviderRequest) -> tuple[list[dict[str, Any]], str | None]:
        last_assistant_index = -1
        previous_response_id: str | None = None

        for index in range(len(request.messages) - 1, -1, -1):
            message = request.messages[index]
            if message.get("role") != "assistant":
                continue
            meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
            if meta.get("provider") != self.provider_id:
                continue
            previous_response_id = meta.get("provider_message_id")
            if previous_response_id:
                last_assistant_index = index
                break

        if previous_response_id:
            input_items: list[dict[str, Any]] = []
            for message in request.messages[last_assistant_index + 1 :]:
                input_items.extend(self._serialize_followup_message(message))
            return input_items, previous_response_id

        if any(message.get("role") == "assistant" for message in request.messages):
            raise ValueError(
                "OpenAI Responses sessions require provider_message_id on prior assistant messages; start a new session"
            )

        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            input_items.extend(self._serialize_user_message(message))
        return input_items, None

    def _serialize_user_message(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        if message.get("role") != "user":
            return []

        text_blocks = [
            block for block in message.get("content") or [] if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not text_blocks:
            return []

        return [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": str(block.get("text") or "")} for block in text_blocks],
            }
        ]

    def _serialize_followup_message(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        if message.get("role") != "user":
            return []

        items: list[dict[str, Any]] = []
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]
        text_blocks = [block for block in blocks if block.get("type") == "text"]
        if text_blocks:
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": str(block.get("text") or "")} for block in text_blocks],
                }
            )

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id") or "",
                    "output": str(block.get("content") or ""),
                }
            )

        return items

    def _serialize_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            "strict": True,
        }

    def _convert_final_response(self, response: Any) -> dict[str, Any]:
        blocks = []
        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)

            if item_type == "reasoning":
                text = _extract_reasoning_text(item)
                meta = {
                    "item_id": getattr(item, "id", None),
                    "status": getattr(item, "status", None),
                }
                summary = _dump_model(getattr(item, "summary", None))
                if summary:
                    meta["summary"] = summary
                blocks.append(
                    thinking_block(text, meta={key: value for key, value in meta.items() if value is not None})
                )
                continue

            if item_type == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) != "output_text":
                        continue
                    meta = {}
                    annotations = _dump_model(getattr(part, "annotations", None))
                    if annotations:
                        meta["annotations"] = annotations
                    blocks.append(text_block(getattr(part, "text", ""), meta=meta or None))
                continue

            if item_type == "function_call":
                raw_arguments = getattr(item, "arguments", "") or ""
                parsed_arguments = parse_tool_arguments(raw_arguments)
                meta = {
                    "item_id": getattr(item, "id", None),
                    "status": getattr(item, "status", None),
                }
                if isinstance(parsed_arguments, str):
                    tool_input = {}
                    meta["raw_arguments"] = raw_arguments
                else:
                    tool_input = parsed_arguments
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(item, "call_id", ""),
                        name=getattr(item, "name", ""),
                        input=tool_input,
                        meta={key: value for key, value in meta.items() if value is not None},
                    )
                )

        meta = {
            "provider": self.provider_id,
            "model": getattr(response, "model", None),
            "provider_message_id": getattr(response, "id", None),
            "status": getattr(response, "status", None),
            "usage": _dump_model(getattr(response, "usage", None)),
        }
        return build_message("assistant", blocks, meta={key: value for key, value in meta.items() if value is not None})


def _extract_reasoning_text(item: Any) -> str:
    parts: list[str] = []
    for content in getattr(item, "content", None) or []:
        text = getattr(content, "text", None)
        if text:
            parts.append(text)

    if parts:
        return "".join(parts)

    for summary in getattr(item, "summary", None) or []:
        text = getattr(summary, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _dump_model(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    return value
