"""OpenAI Responses API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any, cast

from openai import APIError, AsyncOpenAI

from mycode.core.messages import ConversationMessage, assistant_message, text_block, thinking_block, tool_use_block
from mycode.core.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
    dump_model,
)
from mycode.core.tools import parse_tool_arguments


class OpenAIResponsesAdapter(ProviderAdapter):
    """Adapter for OpenAI's Responses API."""

    provider_id = "openai"
    label = "OpenAI Responses"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)
    default_models = ("gpt-5.4", "gpt-5.4-mini")
    supports_reasoning_effort = True

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
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
        prepared_messages = self.prepare_messages(request)
        input_items: list[dict[str, Any]] = []
        for message in prepared_messages:
            role = message.get("role")
            if role == "user":
                input_items.extend(self._serialize_user_message(message))
                continue

            if role != "assistant":
                continue

            native_output_items = self._native_output_items(message)
            if native_output_items is not None:
                input_items.extend(native_output_items)
                continue

            input_items.extend(self._serialize_fallback_assistant_message(message))

        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "instructions": request.system or None,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": request.session_id or None,
            "max_output_tokens": request.max_tokens,
            "tools": [self._serialize_tool(tool) for tool in request.tools] or None,
            "tool_choice": "auto" if request.tools else None,
        }
        if request.reasoning_effort:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        return {key: value for key, value in payload.items() if value is not None}

    def _serialize_user_message(self, message: ConversationMessage) -> list[dict[str, Any]]:
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
                    "output": str(block.get("model_text") or ""),
                }
            )

        return items

    def _native_output_items(self, message: ConversationMessage) -> list[dict[str, Any]] | None:
        raw_meta = message.get("meta")
        if not isinstance(raw_meta, dict) or raw_meta.get("provider") != self.provider_id:
            return None

        native_meta = raw_meta.get("native")
        output_items = native_meta.get("output_items") if isinstance(native_meta, dict) else None
        if not isinstance(output_items, list) or not output_items:
            return None

        replay_items: list[dict[str, Any]] = []
        for item in cast(list[dict[str, Any]], deepcopy(output_items)):
            item_type = str(item.get("type") or "")
            item.pop("status", None)
            if item_type != "reasoning":
                item.pop("id", None)
            replay_items.append(item)

        return replay_items

    def _serialize_fallback_assistant_message(self, message: ConversationMessage) -> list[dict[str, Any]]:
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]
        text_parts = [
            str(block.get("text") or "") for block in blocks if block.get("type") == "text" and block.get("text")
        ]

        items: list[dict[str, Any]] = []
        if text_parts:
            message_item: dict[str, Any] = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "\n".join(text_parts)}],
            }
            items.append(message_item)

        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            call_item: dict[str, Any] = {
                "type": "function_call",
                "call_id": block.get("id") or "",
                "name": block.get("name") or "",
                "arguments": json.dumps(block.get("input") if isinstance(block.get("input"), dict) else {}),
            }
            items.append(call_item)

        return items

    def _serialize_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        parameters = cast(dict[str, Any], dict(tool.get("input_schema") or {"type": "object", "properties": {}}))
        properties = parameters.get("properties")
        required = parameters.get("required")

        # OpenAI strict tools require every top-level property to appear in
        # `required`. Our built-in tool schemas are flat, so optional fields only
        # need a shallow nullable conversion here.
        if isinstance(properties, dict):
            copied_properties: dict[str, Any] = {
                key: dict(value) if isinstance(value, dict) else value for key, value in properties.items()
            }
            required_names = {str(name) for name in required} if isinstance(required, list) else set()

            for name, property_schema in copied_properties.items():
                if name in required_names or not isinstance(property_schema, dict):
                    continue

                property_type = property_schema.get("type")
                if isinstance(property_type, str):
                    property_schema["type"] = [property_type, "null"]
                elif isinstance(property_type, list):
                    if "null" not in property_type:
                        property_schema["type"] = [*property_type, "null"]
                else:
                    copied_properties[name] = {"anyOf": [property_schema, {"type": "null"}]}
                    continue

                enum_values = property_schema.get("enum")
                if isinstance(enum_values, list) and None not in enum_values:
                    property_schema["enum"] = [*enum_values, None]

            parameters["properties"] = copied_properties
            parameters["required"] = list(copied_properties.keys())

        return {
            "type": "function",
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "parameters": parameters,
            "strict": True,
        }

    def _convert_final_response(self, response: Any) -> dict[str, Any]:
        output_items = dump_model(getattr(response, "output", None))
        blocks = []
        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)

            if item_type == "reasoning":
                text_parts = []
                for content in getattr(item, "content", None) or []:
                    text = getattr(content, "text", None)
                    if text:
                        text_parts.append(text)

                if not text_parts:
                    for summary in getattr(item, "summary", None) or []:
                        text = getattr(summary, "text", None)
                        if text:
                            text_parts.append(text)

                native_meta = {
                    "item_id": getattr(item, "id", None),
                    "status": getattr(item, "status", None),
                }
                summary = dump_model(getattr(item, "summary", None))
                if summary:
                    native_meta["summary"] = summary
                filtered_native_meta = {key: value for key, value in native_meta.items() if value is not None}
                blocks.append(
                    thinking_block(
                        "".join(text_parts),
                        meta={"native": filtered_native_meta} if filtered_native_meta else None,
                    )
                )
                continue

            if item_type == "message":
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) != "output_text":
                        continue
                    native_meta = {}
                    annotations = dump_model(getattr(part, "annotations", None))
                    if annotations:
                        native_meta["annotations"] = annotations
                    blocks.append(
                        text_block(
                            getattr(part, "text", ""),
                            meta={"native": native_meta} if native_meta else None,
                        )
                    )
                continue

            if item_type == "function_call":
                raw_arguments = getattr(item, "arguments", "") or ""
                parsed_arguments = parse_tool_arguments(raw_arguments)
                native_meta = {
                    "item_id": getattr(item, "id", None),
                    "status": getattr(item, "status", None),
                }
                if isinstance(parsed_arguments, str):
                    tool_input = {}
                    native_meta["raw_arguments"] = raw_arguments
                else:
                    tool_input = parsed_arguments
                filtered_native_meta = {key: value for key, value in native_meta.items() if value is not None}
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(item, "call_id", ""),
                        name=getattr(item, "name", ""),
                        input=tool_input,
                        meta={"native": filtered_native_meta} if filtered_native_meta else None,
                    )
                )

        return assistant_message(
            blocks,
            provider=self.provider_id,
            model=getattr(response, "model", None),
            provider_message_id=getattr(response, "id", None),
            stop_reason=getattr(response, "status", None),
            usage=dump_model(getattr(response, "usage", None)),
            native_meta={"output_items": output_items} if output_items else None,
        )
