"""Anthropic Messages adapters built on the official Anthropic Python SDK."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from anthropic import APIError, AsyncAnthropic

from mycode.core.messages import (
    ConversationMessage,
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
    dump_model,
    get_native_meta,
    load_document_block_payload,
    load_image_block_payload,
    tool_result_content_blocks,
)

# Maps reasoning_effort values to extended thinking budget_tokens.
_THINKING_BUDGETS: dict[str, int] = {"low": 2048, "medium": 8192, "high": 24576, "xhigh": 32768}


class AnthropicLikeAdapter(ProviderAdapter):
    """Shared Messages adapter for Anthropic-compatible providers.

    Anthropic, Moonshot, and MiniMax all document agent usage around the
    Anthropic Messages protocol. The differences we care about are limited to:

    - default base URL
    - API key env var names
    - optional thinking defaults
    - provider-native metadata carried in content blocks

    MiniMax requires the full assistant content (all blocks) to be sent on
    multi-turn tool-loop requests — not just the text portion.
    """

    def thinking_config(self, _request: ProviderRequest) -> dict[str, Any] | None:
        return None

    def output_config(self, _request: ProviderRequest) -> dict[str, Any] | None:
        return None

    def manual_thinking_config(self, effort: str | None) -> dict[str, Any] | None:
        if not effort:
            return None
        if effort == "none":
            return {"type": "disabled"}
        budget = _THINKING_BUDGETS.get(effort)
        return {"type": "enabled", "budget_tokens": budget} if budget else None

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        messages = [self._serialize_message(message) for message in self.prepare_messages(request)]
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
                    "cache_control": {"type": "ephemeral"},
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

    def project_tool_call_id(self, tool_call_id: str, used_tool_call_ids: set[str]) -> str:
        """Return a short ASCII ID without introducing collisions.

        Anthropic-compatible endpoints only accept IDs containing letters,
        numbers, underscores, and dashes. When projection changes the original
        ID, append a short hash so distinct canonical IDs stay distinct.
        """

        safe_id = "".join(char if char.isalnum() or char in "_-" else "_" for char in tool_call_id)
        if safe_id == tool_call_id and len(safe_id) <= 64 and safe_id not in used_tool_call_ids:
            return safe_id

        prefix = (safe_id or "tool")[:55]
        digest = hashlib.sha1(tool_call_id.encode("utf-8")).hexdigest()[:8]
        candidate = f"{prefix}_{digest}"
        if candidate not in used_tool_call_ids:
            return candidate

        counter = 2
        while True:
            suffix = f"_{digest}_{counter}"
            candidate = f"{(safe_id or 'tool')[: 64 - len(suffix)]}{suffix}"
            if candidate not in used_tool_call_ids:
                return candidate
            counter += 1

    def _apply_cache_control(self, messages: list[dict[str, Any]]) -> None:
        """Mark the last replayed user content block as ephemeral."""

        for message in reversed(messages):
            if message.get("role") != "user":
                continue

            content = message.get("content")
            if not isinstance(content, list):
                return

            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in {"text", "image", "document", "tool_result"}:
                    continue

                block["cache_control"] = {"type": "ephemeral"}
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
            async with client.messages.stream(**self._build_request_payload(request)) as stream:
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
                native_meta = {}
                signature = getattr(block, "signature", None)
                if signature:
                    native_meta["signature"] = signature
                blocks.append(
                    thinking_block(
                        getattr(block, "thinking", ""),
                        meta={"native": native_meta} if native_meta else None,
                    )
                )
                continue

            if block_type == "text":
                native_meta = {}
                citations = getattr(block, "citations", None)
                if citations:
                    native_meta["citations"] = dump_model(citations)
                blocks.append(
                    text_block(getattr(block, "text", ""), meta={"native": native_meta} if native_meta else None)
                )
                continue

            if block_type == "tool_use":
                native_meta = {}
                caller = getattr(block, "caller", None)
                if caller is not None:
                    native_meta["caller"] = caller
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=getattr(block, "input", None),
                        meta={"native": native_meta} if native_meta else None,
                    )
                )
                continue

        native_meta: dict[str, Any] = {}
        if stop_sequence := getattr(message, "stop_sequence", None):
            native_meta["stop_sequence"] = stop_sequence
        if service_tier := getattr(message, "service_tier", None):
            native_meta["service_tier"] = service_tier
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
            native_meta = get_native_meta(block)
            payload: dict[str, Any] = {
                "type": "thinking",
                "thinking": str(block.get("text") or ""),
            }
            if native_meta.get("signature"):
                payload["signature"] = native_meta["signature"]
            return payload

        if block_type == "tool_use":
            native_meta = get_native_meta(block)
            payload = {
                "type": "tool_use",
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") if isinstance(block.get("input"), dict) else {},
            }
            if native_meta.get("caller") is not None:
                payload["caller"] = native_meta["caller"]
            return payload

        if block_type == "image":
            mime_type, data = load_image_block_payload(block)
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": data},
            }

        if block_type == "document":
            mime_type, data, _name = load_document_block_payload(block)
            return {
                "type": "document",
                "source": {"type": "base64", "media_type": mime_type, "data": data},
            }

        if block_type == "tool_result":
            content_blocks = []
            for item in tool_result_content_blocks(block):
                if item.get("type") == "text":
                    content_blocks.append({"type": "text", "text": str(item.get("text") or "")})
                    continue
                if item.get("type") == "image":
                    mime_type, data = load_image_block_payload(item)
                    content_blocks.append(
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": data}}
                    )
            return {
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id"),
                "content": content_blocks or str(block.get("model_text") or ""),
                "is_error": bool(block.get("is_error")),
            }

        return dict(block)


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
        return self.manual_thinking_config(effort)

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
    """Moonshot's Anthropic-compatible Messages endpoint.

    kimi-k2.5 tool loops work through this endpoint. When thinking is enabled,
    prior reasoning blocks must be replayed in the conversation history —
    Moonshot does not strip them on the server side.
    """

    provider_id = "moonshotai"
    label = "Moonshot"
    default_base_url = "https://api.moonshot.ai/anthropic"
    env_api_key_names = ("MOONSHOT_API_KEY",)
    default_models = ("kimi-k2.5",)
    supports_reasoning_effort = True

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        return self.manual_thinking_config(request.reasoning_effort)


class MiniMaxAdapter(AnthropicLikeAdapter):
    """MiniMax's Anthropic-compatible Messages endpoint.

    MiniMax reasoning models emit thinking signatures on this endpoint;
    signatures are preserved in block.meta.native and replayed via
    _serialize_block so multi-turn tool loops stay valid.
    """

    provider_id = "minimax"
    label = "MiniMax"
    default_base_url = "https://api.minimax.io/anthropic"
    env_api_key_names = ("MINIMAX_API_KEY",)
    default_models = ("MiniMax-M2.7", "MiniMax-M2.7-highspeed")
    supports_reasoning_effort = True

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        return self.manual_thinking_config(request.reasoning_effort)
