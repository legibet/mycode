"""OpenAI Responses API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any, cast, override

import httpx
from openai import APIError, AsyncOpenAI

from mycode.messages import (
    ConversationMessage,
    assistant_message,
    build_usage,
    text_block,
    thinking_block,
    tool_use_block,
)
from mycode.providers.base import (
    ProviderAdapter,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    dump_model,
    load_document_block_payload,
    load_image_block_payload,
    native_block_meta,
    normalize_provider_error,
    parse_tool_call_input,
    tool_result_content_blocks,
)
from mycode.utils import omit_none

_RETRYABLE_RESPONSE_ERROR_CODES = {"rate_limit_exceeded", "server_error"}


class OpenAIResponsesAdapter(ProviderAdapter):
    """Adapter for OpenAI's Responses API."""

    provider_id = "openai"
    label = "OpenAI Responses"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)
    default_models = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
    supports_reasoning_effort = True

    @override
    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        api_key = self.require_api_key(request.api_key)

        payload = self._build_request_payload(request)
        try:
            async with AsyncOpenAI(
                api_key=api_key,
                base_url=self.resolve_base_url(request.api_base),
                # connect stays at the SDK's 5s default; retries are owned by
                # the Agent runtime.
                timeout=httpx.Timeout(request.request_timeout, connect=5.0),
                max_retries=0,
            ) as client:
                stream = await client.responses.create(**payload, stream=True)
                async with stream:
                    final_response = None
                    # Some Responses-compatible endpoints emit correct completed output
                    # items during the stream but leave `response.output` empty on the
                    # final completed object. Persist the completed items from the
                    # stream so the canonical assistant message stays intact.
                    streamed_output_items: dict[int, Any] = {}
                    started = False
                    async for event in stream:
                        if not started:
                            started = True
                            yield ProviderStreamEvent("stream_started")
                        event_type = getattr(event, "type", None)

                        if event_type == "response.reasoning_summary_text.delta":
                            delta = cast(str | None, getattr(event, "delta", None))
                            if delta:
                                yield ProviderStreamEvent("thinking_delta", {"text": delta})
                            continue

                        if event_type == "response.output_text.delta":
                            delta = cast(str | None, getattr(event, "delta", None))
                            if delta:
                                yield ProviderStreamEvent("text_delta", {"text": delta})
                            continue

                        if event_type == "response.output_item.done":
                            item = getattr(event, "item", None)
                            if item is not None:
                                output_index = int(getattr(event, "output_index", 0) or 0)
                                streamed_output_items[output_index] = item
                            continue

                        if event_type in {"error", "response.failed"}:
                            error = event
                            if event_type == "response.failed":
                                response = getattr(event, "response", None)
                                error = getattr(response, "error", None) or response or event
                            raise ProviderError(
                                str(getattr(error, "message", None) or error),
                                retryable=getattr(error, "code", None) in _RETRYABLE_RESPONSE_ERROR_CODES,
                            )

                        if event_type == "response.completed":
                            final_response = getattr(event, "response", None)

                    if final_response is None:
                        raise ProviderError("OpenAI Responses stream ended before response.completed")

                    yield ProviderStreamEvent(
                        "message_done",
                        {
                            "message": self._convert_final_response(
                                final_response,
                                output_items=[streamed_output_items[index] for index in sorted(streamed_output_items)]
                                or None,
                                request_model=request.model,
                            )
                        },
                    )
        except (APIError, httpx.HTTPError) as exc:
            raise normalize_provider_error(exc, self.provider_id) from exc

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        for message in self.prepare_messages(request):
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
            "temperature": request.temperature,
            "tools": [self._serialize_tool(tool) for tool in request.tools] or None,
            "tool_choice": "auto" if request.tools else None,
        }
        if request.reasoning_effort:
            reasoning: dict[str, str] = {"effort": request.reasoning_effort}
            if request.reasoning_effort != "none":
                reasoning["summary"] = "auto"
            payload["reasoning"] = reasoning
        return omit_none(payload)

    def _serialize_user_message(self, message: ConversationMessage) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]
        message_content = self._serialize_input_content(
            [block for block in blocks if block.get("type") in {"text", "image", "document"}]
        )
        if message_content:
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": message_content,
                }
            )

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            result_blocks = tool_result_content_blocks(block)
            has_images = any(item.get("type") == "image" for item in result_blocks)
            if has_images:
                output: str | list[dict[str, Any]] = self._serialize_input_content(result_blocks)
            else:
                output = str(block.get("output") or "")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id") or "",
                    "output": output,
                }
            )

        return items

    def _serialize_input_content(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                content.append({"type": "input_text", "text": str(block.get("text") or "")})
                continue
            if block_type == "image":
                mime_type, data = load_image_block_payload(block)
                content.append({"type": "input_image", "image_url": f"data:{mime_type};base64,{data}"})
                continue
            if block_type == "document":
                mime_type, data, name = load_document_block_payload(block)
                content.append(
                    {
                        "type": "input_file",
                        "filename": name or "document.pdf",
                        "file_data": f"data:{mime_type};base64,{data}",
                    }
                )
        return content

    def _native_output_items(self, message: ConversationMessage) -> list[dict[str, Any]] | None:
        """Replay stored OpenAI output items when history already came from Responses."""

        raw_meta = message.get("meta")
        if not isinstance(raw_meta, dict):
            return None

        native_meta = raw_meta.get("native")
        output_items = native_meta.get("output_items") if isinstance(native_meta, dict) else None
        if not isinstance(output_items, list) or not output_items:
            return None

        replay_items: list[dict[str, Any]] = []
        for item in cast(list[dict[str, Any]], deepcopy(output_items)):
            item_type = str(item.get("type") or "")
            item.pop("status", None)  # some gateways don't expect this field in input items
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
        parameters = deepcopy(cast(dict[str, Any], tool.get("input_schema") or {"type": "object", "properties": {}}))
        _normalize_strict_schema(parameters)

        return {
            "type": "function",
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "parameters": parameters,
            "strict": True,
        }

    def _convert_final_response(
        self,
        response: Any,
        *,
        output_items: list[Any] | None = None,
        request_model: str | None = None,
    ) -> ConversationMessage:
        raw_output = output_items if output_items is not None else (getattr(response, "output", None) or [])
        dumped_output_items = dump_model(raw_output)
        blocks: list[dict[str, Any]] = []
        for item in raw_output:
            item_type = getattr(item, "type", None)

            if item_type == "reasoning":
                text_parts = []
                for summary in getattr(item, "summary", None) or []:
                    text = getattr(summary, "text", None)
                    if text:
                        text_parts.append(text)

                summary = dump_model(getattr(item, "summary", None))
                item_meta = omit_none(
                    {
                        "item_id": getattr(item, "id", None),
                        "status": getattr(item, "status", None),
                        "summary": summary or None,
                    }
                )
                blocks.append(
                    thinking_block(
                        "".join(text_parts),
                        meta=native_block_meta(item_meta),
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
                            meta=native_block_meta(native_meta),
                        )
                    )
                continue

            if item_type == "function_call":
                tool_input, extra_native = parse_tool_call_input(getattr(item, "arguments", "") or "")
                item_meta = omit_none(
                    {
                        "item_id": getattr(item, "id", None),
                        "status": getattr(item, "status", None),
                        **extra_native,
                    }
                )
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(item, "call_id", ""),
                        name=getattr(item, "name", ""),
                        input=tool_input,
                        meta=native_block_meta(item_meta),
                    )
                )

        raw_usage = dump_model(getattr(response, "usage", None)) or {}
        input_details = raw_usage.get("input_tokens_details") or {}
        output_details = raw_usage.get("output_tokens_details") or {}

        return assistant_message(
            blocks,
            provider=self.provider_id,
            model=request_model or getattr(response, "model", None),
            provider_message_id=getattr(response, "id", None),
            stop_reason=getattr(response, "status", None),
            usage=build_usage(
                total_tokens=raw_usage.get("total_tokens"),
                input_tokens=raw_usage.get("input_tokens"),
                cache_read_tokens=input_details.get("cached_tokens"),
                cache_write_tokens=input_details.get("cache_write_tokens"),
                output_tokens=raw_usage.get("output_tokens"),
                reasoning_tokens=output_details.get("reasoning_tokens"),
            ),
            native_meta={
                "output_items": dumped_output_items or None,
                "usage": raw_usage or None,
            },
        )


def _normalize_strict_schema(schema: Any) -> None:
    """Mutate a JSON schema into the strict shape required by OpenAI tools."""

    if isinstance(schema, list):
        for item in schema:
            _normalize_strict_schema(item)
        return
    if not isinstance(schema, dict):
        return

    schema.pop("default", None)
    additional_properties = schema.get("additionalProperties")
    if additional_properties not in (None, False):
        raise ValueError("OpenAI strict tool schemas do not support object schemas with dynamic keys")

    properties = schema.get("properties")
    if isinstance(properties, dict):
        required_names = {name for name in schema.get("required", []) if isinstance(name, str)}
        for name, property_schema in properties.items():
            _normalize_strict_schema(property_schema)
            if name not in required_names:
                properties[name] = _nullable_schema(property_schema)
        schema["required"] = list(properties.keys())
        schema["additionalProperties"] = False

    for key in ("$defs", "defs"):
        defs = schema.get(key)
        if isinstance(defs, dict):
            for definition in defs.values():
                _normalize_strict_schema(definition)

    for key in ("items", "anyOf", "oneOf", "allOf"):
        _normalize_strict_schema(schema.get(key))


def _nullable_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"anyOf": [schema, {"type": "null"}]}

    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        if schema_type == "null":
            return schema
        nullable_type = [schema_type, "null"]
    elif isinstance(schema_type, list):
        if "null" in schema_type:
            return schema
        nullable_type = [*schema_type, "null"]
    else:
        any_of = schema.get("anyOf")
        if isinstance(any_of, list) and any(isinstance(item, dict) and item.get("type") == "null" for item in any_of):
            return schema
        return {"anyOf": [schema, {"type": "null"}]}

    nullable = {**schema, "type": nullable_type}
    if "const" in nullable:
        nullable["enum"] = [nullable.pop("const"), None]
    elif isinstance(nullable.get("enum"), list) and None not in nullable["enum"]:
        nullable["enum"] = [*nullable["enum"], None]
    return nullable
