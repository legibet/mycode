"""Responses API adapters for OpenAI and xAI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from typing import Any, cast, override

import httpx2

from mycode.messages import (
    ContentBlock,
    ConversationMessage,
    StopReason,
    assistant_message,
    text_block,
    thinking_block,
    tool_use_block,
)
from mycode.providers.base import (
    MessageDone,
    ProviderAdapter,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    StreamStarted,
    TextDelta,
    ThinkingDelta,
    dump_model,
    native_block_meta,
    normalize_provider_error,
    parse_tool_call_input,
    tool_result_content_blocks,
)
from mycode.tools import ToolDefinition
from mycode.usage import build_usage

_RETRYABLE_RESPONSE_ERROR_CODES = {"rate_limit_exceeded", "server_error"}


def _normalize_response_status(response: Any, *, has_tool_calls: bool) -> StopReason:
    status = str(getattr(response, "status", None) or "").lower()
    if status == "completed":
        return "tool_use" if has_tool_calls else "stop"
    if status == "incomplete":
        reason = str(getattr(getattr(response, "incomplete_details", None), "reason", None) or "").lower()
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "error"
    if status == "failed":
        return "error"
    return "unknown"


class OpenAIResponsesAdapter(ProviderAdapter):
    """Adapter for OpenAI's Responses API."""

    provider_id = "openai"
    label = "OpenAI Responses"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)
    default_models = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")
    supports_reasoning_effort = True
    _thinking_delta_types: tuple[str, ...] = ("response.reasoning_summary_text.delta",)
    _reasoning_text_fields: tuple[str, ...] = ("summary",)

    @override
    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        from openai import APIError, AsyncOpenAI

        api_key = self.require_api_key(request.api_key)

        payload = self._build_request_payload(request)
        try:
            async with AsyncOpenAI(
                api_key=api_key,
                base_url=self.resolve_base_url(request.api_base),
                # connect stays at the SDK's 5s default; retries are owned by
                # the Agent runtime.
                timeout=httpx2.Timeout(request.request_timeout, connect=5.0),
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
                            yield StreamStarted()
                        event_type = getattr(event, "type", None)

                        if event_type in self._thinking_delta_types:
                            delta = cast(str | None, getattr(event, "delta", None))
                            if delta:
                                yield ThinkingDelta(delta)
                            continue

                        if event_type == "response.output_text.delta":
                            delta = cast(str | None, getattr(event, "delta", None))
                            if delta:
                                yield TextDelta(delta)
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

                        if event_type in {"response.completed", "response.incomplete"}:
                            final_response = getattr(event, "response", None)
                            # Keep consuming the stream: the SDK closes its nested
                            # response streams only once iteration finishes.

                    if final_response is None:
                        raise ProviderError(f"{self.label} stream ended before response.completed")

                    yield MessageDone(
                        self._convert_final_response(
                            final_response,
                            output_items=[streamed_output_items[index] for index in sorted(streamed_output_items)]
                            or None,
                            request_model=request.model,
                        )
                    )
        except (APIError, httpx2.HTTPError) as exc:
            raise normalize_provider_error(exc, self.provider_id) from exc

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        for message in self.prepare_messages(request):
            if message["role"] == "user":
                input_items.extend(self._serialize_user_message(message))
            elif native_output_items := self._native_output_items(message):
                input_items.extend(native_output_items)
            else:
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
        reasoning: dict[str, str] = {}
        if request.reasoning_effort:
            reasoning["effort"] = request.reasoning_effort
        if request.reasoning_effort != "none":
            reasoning["summary"] = "auto"
        payload["reasoning"] = reasoning
        return {key: value for key, value in payload.items() if value is not None}

    def _serialize_user_message(self, message: ConversationMessage) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        message_content = self._serialize_input_content(message["content"])
        if message_content:
            items.append({"type": "message", "role": "user", "content": message_content})

        for block in message["content"]:
            if block["type"] != "tool_result":
                continue
            result_blocks = tool_result_content_blocks(block)
            output: str | list[dict[str, Any]] = block["output"]
            if any(item["type"] == "image" for item in result_blocks):
                output = self._serialize_input_content(result_blocks)
            items.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": output})

        return items

    def _serialize_input_content(self, blocks: Sequence[ContentBlock]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for block in blocks:
            match block["type"]:
                case "text":
                    content.append({"type": "input_text", "text": block["text"]})
                case "image":
                    content.append(
                        {"type": "input_image", "image_url": f"data:{block['mime_type']};base64,{block['data']}"}
                    )
                case "document":
                    content.append(
                        {
                            "type": "input_file",
                            "filename": block.get("name") or "document.pdf",
                            "file_data": f"data:{block['mime_type']};base64,{block['data']}",
                        }
                    )
        return content

    def _native_output_items(self, message: ConversationMessage) -> list[dict[str, Any]]:
        """Replay stored output items when history already came from Responses."""

        invalid_call_ids = {
            block["id"]
            for block in message["content"]
            if block["type"] == "tool_use" and block.get("meta", {}).get("invalid_input") is True
        }

        replay_items: list[dict[str, Any]] = []
        for raw_item in message.get("meta", {}).get("native", {}).get("output_items") or []:
            item = dict(raw_item)
            item.pop("status", None)  # some gateways don't expect this field in input items
            if item["type"] != "reasoning":
                item.pop("id", None)
            if item["type"] == "function_call" and item["call_id"] in invalid_call_ids:
                item["arguments"] = "{}"  # never resend arguments that failed to parse
            replay_items.append(item)

        return replay_items

    def _serialize_fallback_assistant_message(self, message: ConversationMessage) -> list[dict[str, Any]]:
        blocks = message["content"]
        items: list[dict[str, Any]] = []
        texts = [block["text"] for block in blocks if block["type"] == "text" and block["text"]]
        if texts:
            items.append(
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "\n".join(texts)}]}
            )

        items.extend(
            {
                "type": "function_call",
                "call_id": block["id"],
                "name": block["name"],
                "arguments": json.dumps(block["input"]),
            }
            for block in blocks
            if block["type"] == "tool_use"
        )
        return items

    def _serialize_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        parameters = deepcopy(tool["input_schema"])
        _normalize_strict_schema(parameters)

        return {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
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
        # Persist received fields under their wire names; a full dump fills absent optionals with null.
        dumped_output_items = [item.model_dump(exclude_unset=True, by_alias=True) for item in raw_output]
        blocks: list[ContentBlock] = []
        for item in raw_output:
            item_type = getattr(item, "type", None)

            if item_type == "reasoning":
                text_parts = []
                for field in self._reasoning_text_fields:
                    for part in getattr(item, field, None) or []:
                        text = getattr(part, "text", None)
                        if text:
                            text_parts.append(text)
                    if text_parts:
                        break

                summary = dump_model(getattr(item, "summary", None))
                raw_item_meta = {
                    "item_id": getattr(item, "id", None),
                    "status": getattr(item, "status", None),
                    "summary": summary or None,
                }
                item_meta = {key: value for key, value in raw_item_meta.items() if value is not None}
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
                tool_input, invalid_meta = parse_tool_call_input(getattr(item, "arguments", "") or "")
                raw_item_meta = {
                    "item_id": getattr(item, "id", None),
                    "status": getattr(item, "status", None),
                }
                item_meta = {key: value for key, value in raw_item_meta.items() if value is not None}
                block_meta = (native_block_meta(item_meta) or {}) | invalid_meta
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(item, "call_id", ""),
                        name=getattr(item, "name", ""),
                        input=tool_input,
                        meta=block_meta or None,
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
            stop_reason=_normalize_response_status(
                response,
                has_tool_calls=any(block["type"] == "tool_use" for block in blocks),
            ),
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
            },
        )


class XAIAdapter(OpenAIResponsesAdapter):
    """xAI's Responses endpoint."""

    provider_id = "xai"
    label = "xAI"
    default_base_url = "https://api.x.ai/v1"
    env_api_key_names = ("XAI_API_KEY",)
    default_models = ("grok-4.7",)
    _thinking_delta_types = ("response.reasoning_summary_text.delta", "response.reasoning_text.delta")
    _reasoning_text_fields = ("summary", "content")

    @override
    def _serialize_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        # xAI validates arguments against the supplied schema directly; optional
        # parameters keep their defaults and need no strict-mode rewriting.
        return {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        }

    @override
    def _convert_final_response(
        self,
        response: Any,
        *,
        output_items: list[Any] | None = None,
        request_model: str | None = None,
    ) -> ConversationMessage:
        message = super()._convert_final_response(
            response,
            output_items=output_items,
            request_model=request_model,
        )
        raw_usage = dump_model(getattr(response, "usage", None)) or {}
        cost_ticks = raw_usage.get("cost_in_usd_ticks")
        if cost_ticks is not None:
            message["meta"]["cost"] = {"total": cost_ticks / 10_000_000_000}
        return message


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
