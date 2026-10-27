"""Anthropic Messages adapters built on the official Anthropic Python SDK."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any, cast, override

import httpx2

from mycode.messages import (
    ContentBlock,
    ConversationMessage,
    DocumentBlock,
    ImageBlock,
    StopReason,
    assistant_message,
    text_block,
    thinking_block,
    tool_use_block,
)
from mycode.providers.base import (
    MessageDone,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
    StreamStarted,
    TextDelta,
    ThinkingDelta,
    dump_model,
    get_native_meta,
    native_block_meta,
    normalize_provider_error,
    tool_result_content_blocks,
)
from mycode.tools import ToolDefinition
from mycode.usage import build_usage

_RETRYABLE_ANTHROPIC_ERROR_TYPES = {"api_error", "overloaded_error", "rate_limit_error", "timeout_error"}


def _normalize_stop_reason(raw_reason: Any) -> StopReason:
    reason = str(raw_reason or "").lower()
    if reason == "tool_use":
        return "tool_use"
    if reason == "max_tokens":
        return "length"
    if reason in {"end_turn", "stop_sequence"}:
        return "stop"
    return "unknown"


class AnthropicLikeAdapter(ProviderAdapter):
    """Shared Messages adapter for Anthropic-compatible providers."""

    supports_reasoning_effort = True
    thinking: dict[str, Any] | None = None

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        messages = []
        for replay_message in self.prepare_messages(request):
            message = self._serialize_message(replay_message)
            if message["content"]:
                messages.append(message)
        # Serialized user messages contain only cacheable blocks and are non-empty.
        for message in reversed(messages):
            if message["role"] == "user":
                message["content"][-1]["cache_control"] = {"type": "ephemeral"}
                break

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
        if self.thinking is not None:
            payload["thinking"] = self.thinking
        if request.reasoning_effort:
            payload["output_config"] = {"effort": request.reasoning_effort}
        return payload

    @override
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

    @override
    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        from anthropic import APIError, AsyncAnthropic

        api_key = self.require_api_key(request.api_key)

        try:
            async with (
                AsyncAnthropic(
                    api_key=api_key,
                    base_url=self.resolve_base_url(request.api_base),
                    # connect stays at the SDK's 5s default; retries are owned
                    # by the Agent runtime.
                    timeout=httpx2.Timeout(request.request_timeout, connect=5.0),
                    max_retries=0,
                ) as client,
                client.messages.stream(**self._build_request_payload(request)) as stream,
            ):
                started = False
                async for event in stream:
                    if not started:
                        started = True
                        yield StreamStarted()
                    event_type = getattr(event, "type", None)
                    if event_type == "thinking":
                        thinking = cast(str | None, getattr(event, "thinking", None))
                        if thinking:
                            yield ThinkingDelta(thinking)
                        continue
                    if event_type == "text":
                        text = cast(str | None, getattr(event, "text", None))
                        if text:
                            yield TextDelta(text)

                final_message = await stream.get_final_message()
        except (APIError, httpx2.HTTPError) as exc:
            error = normalize_provider_error(exc, self.provider_id)
            if self.provider_id == "anthropic" and getattr(exc, "type", None) in _RETRYABLE_ANTHROPIC_ERROR_TYPES:
                error.retryable = True
            raise error from exc

        yield MessageDone(self._convert_final_message(final_message, request_model=request.model))

    def _convert_final_message(self, message: Any, *, request_model: str | None = None) -> ConversationMessage:
        blocks: list[ContentBlock] = []
        for block in getattr(message, "content", []) or []:
            block_type = getattr(block, "type", None)

            if block_type in {"thinking", "redacted_thinking"}:
                blocks.append(
                    thinking_block(
                        getattr(block, "thinking", "") if block_type == "thinking" else "",
                        meta=native_block_meta({"anthropic_block": dump_model(block)}),
                    )
                )
                continue

            if block_type == "text":
                native_meta = {}
                citations = getattr(block, "citations", None)
                if citations:
                    native_meta["citations"] = dump_model(citations)
                blocks.append(text_block(getattr(block, "text", ""), meta=native_block_meta(native_meta)))
                continue

            if block_type == "tool_use":
                blocks.append(
                    tool_use_block(
                        tool_id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        input=getattr(block, "input", None),
                    )
                )
                continue

        message_native_meta: dict[str, Any] = {}
        if stop_sequence := getattr(message, "stop_sequence", None):
            message_native_meta["stop_sequence"] = stop_sequence
        if service_tier := getattr(message, "service_tier", None):
            message_native_meta["service_tier"] = service_tier

        raw_usage = dump_model(getattr(message, "usage", None)) or {}

        # `input_tokens` excludes cache tokens on this protocol; the canonical
        # input is the sum of all three. A compatible upstream omitting cache
        # fields leaves the sum unknown rather than understated.
        base_input = raw_usage.get("input_tokens")
        cache_write = raw_usage.get("cache_creation_input_tokens")
        cache_read = raw_usage.get("cache_read_input_tokens")
        if base_input is None or cache_write is None or cache_read is None:
            input_tokens = None
        else:
            input_tokens = base_input + cache_write + cache_read

        return assistant_message(
            blocks,
            provider=self.provider_id,
            model=request_model or getattr(message, "model", None),
            provider_message_id=getattr(message, "id", None),
            stop_reason=_normalize_stop_reason(getattr(message, "stop_reason", None)),
            usage=build_usage(
                input_tokens=input_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                output_tokens=raw_usage.get("output_tokens"),
                reasoning_tokens=(raw_usage.get("output_tokens_details") or {}).get("thinking_tokens"),
            ),
            native_meta=message_native_meta,
        )

    def _serialize_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        return {"name": tool["name"], "description": tool["description"], "input_schema": tool["input_schema"]}

    def _serialize_message(self, message: ConversationMessage) -> dict[str, Any]:
        content = []
        for block in message["content"]:
            if block["type"] == "thinking":
                # Thinking is sent back only as the original block the provider
                # returned, because the endpoint checks that block's signature.
                # A thinking block recorded without it would be rejected, so it
                # is left out.
                if native_block := get_native_meta(block).get("anthropic_block"):
                    content.append(dict(native_block))
                continue
            content.append(self._serialize_block(block))
        return {"role": message["role"], "content": content}

    def _serialize_block(self, block: ContentBlock) -> dict[str, Any]:
        match block["type"]:
            case "text":
                return {"type": "text", "text": block["text"]}
            case "tool_use":
                return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block["input"]}
            case "image" | "document":
                return {"type": block["type"], "source": _base64_source(block)}
            case "tool_result":
                # Tool content is application-defined; only text and images are sent.
                content_blocks = []
                for item in tool_result_content_blocks(block):
                    if item["type"] == "text":
                        content_blocks.append({"type": "text", "text": item["text"]})
                    elif item["type"] == "image":
                        content_blocks.append({"type": "image", "source": _base64_source(item)})
                return {
                    "type": "tool_result",
                    "tool_use_id": block["tool_use_id"],
                    "content": content_blocks or block["output"],
                    "is_error": block["is_error"],
                }
        raise ValueError(f"unsupported content block type: {block['type']}")


def _base64_source(block: ImageBlock | DocumentBlock) -> dict[str, Any]:
    return {"type": "base64", "media_type": block["mime_type"], "data": block["data"]}


class AnthropicAdapter(AnthropicLikeAdapter):
    provider_id = "anthropic"
    label = "Anthropic"
    default_base_url = "https://api.anthropic.com"
    env_api_key_names = ("ANTHROPIC_API_KEY",)
    default_models = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
    # Current Claude models return empty thinking text by default; "summarized"
    # asks the API to return a readable summary of the model's reasoning.
    thinking = {"type": "adaptive", "display": "summarized"}


class MoonshotAIAdapter(AnthropicLikeAdapter):
    """Moonshot's Anthropic-compatible Messages endpoint.

    Kimi K3 models always reason, and this endpoint has no setting to turn
    reasoning on or off, so requests do not include a `thinking` field.
    Earlier reasoning blocks must be sent back in the conversation history,
    because Moonshot does not keep them on the server side.
    """

    provider_id = "moonshotai"
    label = "Moonshot"
    default_base_url = "https://api.moonshot.ai/anthropic"
    env_api_key_names = ("MOONSHOT_API_KEY",)
    default_models = ("kimi-k3.1-pro", "kimi-k3.1-pro-highspeed")


class MiniMaxAdapter(AnthropicLikeAdapter):
    """MiniMax's Anthropic-compatible Messages endpoint.

    MiniMax-M3 only reasons when the request includes
    `thinking: {type: "adaptive"}`, so every request sends it.
    MiniMax-M3.1-Flash always reasons and accepts the same field.
    """

    provider_id = "minimax"
    label = "MiniMax"
    default_base_url = "https://api.minimax.io/anthropic"
    env_api_key_names = ("MINIMAX_API_KEY",)
    default_models = ("MiniMax-M3.1-Flash", "MiniMax-M3")
    thinking = {"type": "adaptive"}
