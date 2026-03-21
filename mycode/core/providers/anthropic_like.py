"""Anthropic Messages adapters built on the official Anthropic Python SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from anthropic import APIError, AsyncAnthropic

from mycode.core.messages import (
    ConversationMessage,
    assistant_message,
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
    dump_model,
)

_MANUAL_THINKING_BUDGETS = {
    "low": 2048,
    "medium": 8192,
    "high": 24576,
    "xhigh": 32768,
}

CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}


class AnthropicLikeAdapter(ProviderAdapter):
    """Shared Messages adapter for Anthropic-compatible providers.

    Anthropic, Moonshot, and MiniMax all document agent usage around the
    Anthropic Messages protocol. The differences we care about are limited to:

    - default base URL
    - API key env var names
    - optional thinking defaults
    - provider-native metadata carried in content blocks
    """

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        return None

    def output_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        return None

    def build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        messages = [self._serialize_message(message) for message in request.messages]
        self._apply_cache_control(messages)

        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.system:
            payload["system"] = [
                {
                    "type": "text",
                    "text": request.system,
                    "cache_control": dict(CACHE_CONTROL_EPHEMERAL),
                }
            ]
        if request.tools:
            payload["tools"] = [self._serialize_tool(tool) for tool in request.tools]
            payload["tool_choice"] = {"type": "auto"}
        thinking = self.thinking_config(request)
        if thinking is not None:
            payload["thinking"] = thinking
        output_config = self.output_config(request)
        if output_config is not None:
            payload["output_config"] = output_config
        return payload

    def _apply_cache_control(self, messages: list[dict[str, Any]]) -> None:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if not isinstance(content, list):
                return

            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in {"text", "image", "tool_result"}:
                    continue

                block["cache_control"] = dict(CACHE_CONTROL_EPHEMERAL)
                return

            return

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
                    meta["citations"] = dump_model(citations)
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

        native_meta = {
            "stop_sequence": getattr(message, "stop_sequence", None),
            "service_tier": getattr(message, "service_tier", None),
        }
        return assistant_message(
            blocks,
            provider=self.provider_id,
            model=getattr(message, "model", None),
            provider_message_id=getattr(message, "id", None),
            stop_reason=getattr(message, "stop_reason", None),
            usage=dump_model(getattr(message, "usage", None)),
            native_meta=native_meta,
        )

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
            raw_meta = block.get("meta")
            meta: dict[str, Any] = {}
            if isinstance(raw_meta, dict):
                meta = raw_meta
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
            raw_meta = block.get("meta")
            meta: dict[str, Any] = {}
            if isinstance(raw_meta, dict):
                meta = raw_meta
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


class AnthropicAdapter(AnthropicLikeAdapter):
    provider_id = "anthropic"
    label = "Anthropic"
    default_base_url = "https://api.anthropic.com"
    env_api_key_names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    default_models = ("claude-sonnet-4-6", "claude-opus-4-6")
    supports_reasoning_effort = True

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = request.reasoning_effort
        if not effort:
            return None
        if effort == "none":
            return {"type": "disabled"}

        normalized = request.model.lower()
        if normalized.startswith("claude-sonnet-4-6") or normalized.startswith("claude-opus-4-6"):
            return {"type": "adaptive"}

        budget = _MANUAL_THINKING_BUDGETS.get(effort)
        return {"type": "enabled", "budget_tokens": budget} if budget is not None else None

    def output_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = request.reasoning_effort
        if not effort or effort == "none":
            return None

        normalized = request.model.lower()
        if normalized.startswith("claude-sonnet-4-6"):
            mapped_effort = "high" if effort == "xhigh" else effort
            return {"effort": mapped_effort}

        if normalized.startswith("claude-opus-4-6"):
            mapped_effort = "max" if effort == "xhigh" else effort
            return {"effort": mapped_effort}

        return None


class MoonshotAIAdapter(AnthropicLikeAdapter):
    provider_id = "moonshotai"
    label = "Moonshot"
    default_base_url = "https://api.moonshot.ai/anthropic"
    env_api_key_names = ("MOONSHOT_API_KEY",)
    default_models = ("kimi-k2.5",)
    supports_reasoning_effort = True

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = request.reasoning_effort
        if not effort:
            return None
        if effort == "none":
            return {"type": "disabled"}
        budget = _MANUAL_THINKING_BUDGETS.get(effort)
        return {"type": "enabled", "budget_tokens": budget} if budget is not None else None


class MiniMaxAdapter(AnthropicLikeAdapter):
    provider_id = "minimax"
    label = "MiniMax"
    default_base_url = "https://api.minimax.io/anthropic"
    env_api_key_names = ("MINIMAX_API_KEY",)
    default_models = ("MiniMax-M2.7", "MiniMax-M2.7-highspeed")
    supports_reasoning_effort = True

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = request.reasoning_effort
        if not effort:
            return None
        if effort == "none":
            return {"type": "disabled"}
        budget = _MANUAL_THINKING_BUDGETS.get(effort)
        return {"type": "enabled", "budget_tokens": budget} if budget is not None else None
