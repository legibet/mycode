"""Anthropic Messages adapters built on the official Anthropic Python SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from anthropic import APIError, AsyncAnthropic

from mycode.core.messages import (
    ConversationMessage,
    build_message,
    text_block,
    thinking_block,
    tool_result_block,
    tool_use_block,
)
from mycode.core.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
)

THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
}


class AnthropicLikeAdapter(ProviderAdapter):
    """Shared Messages adapter for Anthropic-compatible providers.

    Anthropic, Moonshot, and MiniMax all document agent usage around the
    Anthropic Messages protocol. The differences we care about are limited to:

    - default base URL
    - API key env var names
    - optional thinking defaults
    - provider-native metadata carried in content blocks
    """

    def build_thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        return None

    def build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [self._serialize_message(message) for message in request.messages],
        }
        if request.system:
            payload["system"] = request.system
        if request.tools:
            payload["tools"] = [self._serialize_tool(tool) for tool in request.tools]
            payload["tool_choice"] = {"type": "auto"}
        thinking = self.build_thinking_config(request)
        if thinking is not None:
            payload["thinking"] = thinking
        return payload

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        api_key = self.require_api_key(request.api_key)
        client = AsyncAnthropic(
            api_key=api_key,
            base_url=self.resolve_base_url(request.api_base),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

        try:
            async with client.messages.stream(**self.build_request_payload(request)) as stream:
                async for event in stream:
                    if event.type == "thinking" and event.thinking:
                        yield ProviderStreamEvent("thinking_delta", {"text": event.thinking})
                        continue
                    if event.type == "text" and event.text:
                        yield ProviderStreamEvent("text_delta", {"text": event.text})

                final_message = await stream.get_final_message()
        except APIError as exc:
            raise ValueError(str(exc)) from exc

        yield ProviderStreamEvent(
            "message_done",
            {
                "message": self._convert_final_message(final_message),
            },
        )

    def _convert_final_message(self, message: Any) -> ConversationMessage:
        blocks = []
        for block in getattr(message, "content", []) or []:
            block_type = getattr(block, "type", None)

            if block_type == "thinking":
                meta = {}
                signature = getattr(block, "signature", None)
                if signature:
                    meta["signature"] = signature
                blocks.append(thinking_block(getattr(block, "thinking", ""), meta=meta or None))
                continue

            if block_type == "text":
                meta = {}
                citations = getattr(block, "citations", None)
                if citations:
                    meta["citations"] = _dump_model(citations)
                blocks.append(text_block(getattr(block, "text", ""), meta=meta or None))
                continue

            if block_type == "tool_use":
                meta = {}
                caller = getattr(block, "caller", None)
                if caller is not None:
                    meta["caller"] = caller
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=getattr(block, "input", None),
                        meta=meta or None,
                    )
                )
                continue

            if block_type == "tool_result":
                blocks.append(
                    tool_result_block(
                        tool_use_id=getattr(block, "tool_use_id", ""),
                        content=_stringify_tool_result_content(getattr(block, "content", "")),
                        is_error=bool(getattr(block, "is_error", False)),
                    )
                )

        meta = {
            "provider": self.provider_id,
            "model": getattr(message, "model", None),
            "provider_message_id": getattr(message, "id", None),
            "stop_reason": getattr(message, "stop_reason", None),
            "stop_sequence": getattr(message, "stop_sequence", None),
            "usage": _dump_model(getattr(message, "usage", None)),
        }

        service_tier = getattr(message, "service_tier", None)
        if service_tier is not None:
            meta["service_tier"] = service_tier

        return build_message("assistant", blocks, meta={key: value for key, value in meta.items() if value is not None})

    def _serialize_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": tool.get("name") or "",
            "description": tool.get("description") or "",
            "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
        }

    def _serialize_message(self, message: ConversationMessage) -> dict[str, Any]:
        return {
            "role": str(message.get("role") or "user"),
            "content": [
                self._serialize_block(block) for block in message.get("content") or [] if isinstance(block, dict)
            ],
        }

    def _serialize_block(self, block: dict[str, Any]) -> dict[str, Any]:
        block_type = block.get("type")

        if block_type == "text":
            return {"type": "text", "text": str(block.get("text") or "")}

        if block_type == "thinking":
            payload = {
                "type": "thinking",
                "thinking": str(block.get("text") or ""),
            }
            meta = block.get("meta") if isinstance(block.get("meta"), dict) else {}
            signature = meta.get("signature")
            if signature:
                payload["signature"] = signature
            return payload

        if block_type == "tool_use":
            payload = {
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
            }
            meta = block.get("meta") if isinstance(block.get("meta"), dict) else {}
            if meta.get("caller") is not None:
                payload["caller"] = meta["caller"]
            return payload

        if block_type == "tool_result":
            return {
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id"),
                "content": str(block.get("content") or ""),
                "is_error": bool(block.get("is_error")),
            }

        return dict(block)


def _dump_model(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump_model(item) for item in value]
    return value


def _stringify_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)
