"""Chat Completions adapters for OpenAI-compatible providers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, override

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
    ProviderRequest,
    ProviderStreamEvent,
    dump_model,
    get_native_meta,
    load_document_block_payload,
    load_image_block_payload,
    native_block_meta,
    normalize_provider_error,
    parse_tool_call_input,
)
from mycode.utils import omit_none


@dataclass
class _ChatToolCallState:
    """Accumulate one streamed tool call from chat-completions deltas."""

    tool_id: str | None = None
    name: str = ""
    arguments_text: str = ""


class OpenAIChatAdapter(ProviderAdapter):
    """Base adapter for Chat Completions style providers."""

    provider_id = "openai_chat"
    label = "OpenAI Chat Completions"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)
    auto_discoverable = False

    @override
    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        api_key = self.require_api_key(request.api_key)

        tool_calls: dict[int, _ChatToolCallState] = {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_native_meta: dict[str, Any] = {}
        response_id: str | None = None
        finish_reason: str | None = None
        usage: Any = None

        try:
            async with AsyncOpenAI(
                api_key=api_key,
                base_url=self.resolve_base_url(request.api_base),
                # connect stays at the SDK's 5s default; retries are owned by
                # the Agent runtime.
                timeout=httpx.Timeout(request.request_timeout, connect=5.0),
                max_retries=0,
            ) as client:
                stream = await client.chat.completions.create(**self._build_request_payload(request), stream=True)
                async with stream:
                    started = False
                    async for chunk in stream:
                        if not started:
                            started = True
                            yield ProviderStreamEvent("stream_started")
                        response_id = response_id or getattr(chunk, "id", None)

                        if getattr(chunk, "usage", None) is not None:
                            usage = chunk.usage

                        if not chunk.choices:
                            continue

                        choice = chunk.choices[0]
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason

                        delta = choice.delta
                        reasoning_delta = self._consume_reasoning_delta(
                            thinking_parts,
                            thinking_native_meta,
                            delta,
                        )
                        if reasoning_delta:
                            yield ProviderStreamEvent("thinking_delta", {"text": reasoning_delta})

                        if delta.content:
                            text_parts.append(delta.content)
                            yield ProviderStreamEvent("text_delta", {"text": delta.content})

                        for tool_call in delta.tool_calls or []:
                            index = tool_call.index or 0
                            state = tool_calls.setdefault(index, _ChatToolCallState())
                            if tool_call.id:
                                state.tool_id = tool_call.id
                            function = tool_call.function
                            if function is None:
                                continue
                            if function.name:
                                state.name = function.name
                            if function.arguments:
                                state.arguments_text += function.arguments
        except (APIError, httpx.HTTPError) as exc:
            raise normalize_provider_error(exc, self.provider_id) from exc

        blocks = []
        if thinking_parts or thinking_native_meta:
            blocks.append(
                thinking_block(
                    "".join(thinking_parts),
                    meta=native_block_meta(thinking_native_meta),
                )
            )
        if text_parts:
            blocks.append(text_block("".join(text_parts)))

        for index in sorted(tool_calls):
            state = tool_calls[index]
            tool_input, extra_native = parse_tool_call_input(state.arguments_text)
            blocks.append(
                tool_use_block(
                    tool_id=state.tool_id or f"tool_call_{index}",
                    name=state.name,
                    input=tool_input,
                    meta=native_block_meta(extra_native),
                )
            )

        raw_usage = dump_model(usage) or {}
        prompt_details = raw_usage.get("prompt_tokens_details") or {}
        completion_details = raw_usage.get("completion_tokens_details") or {}
        cache_read_tokens = prompt_details.get("cached_tokens")
        if cache_read_tokens is None:
            # DeepSeek reports cache hits as a top-level extension field.
            cache_read_tokens = raw_usage.get("prompt_cache_hit_tokens")

        final_message = assistant_message(
            blocks,
            provider=self.provider_id,
            model=request.model,
            provider_message_id=response_id,
            stop_reason=finish_reason,
            usage=build_usage(
                total_tokens=raw_usage.get("total_tokens"),
                input_tokens=raw_usage.get("prompt_tokens"),
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=prompt_details.get("cache_write_tokens"),
                output_tokens=raw_usage.get("completion_tokens"),
                reasoning_tokens=completion_details.get("reasoning_tokens"),
                # Only OpenRouter's `cost` extension is known to mean the
                # actually charged USD amount; other upstreams' same-named
                # fields must not short-circuit the estimate.
                cost_usd=raw_usage.get("cost") if self.provider_id == "openrouter" else None,
            ),
            native_meta={"usage": raw_usage or None},
        )
        yield ProviderStreamEvent("message_done", {"message": final_message})

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in self.prepare_messages(request):
            messages.extend(self._serialize_message(message))

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "tools": [self._serialize_tool(tool) for tool in request.tools] or None,
            "tool_choice": "auto" if request.tools else None,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream_options": {"include_usage": True},
        }
        payload.update(self._build_provider_payload_overrides(request))
        return omit_none(payload)

    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        return {"reasoning_effort": request.reasoning_effort} if request.reasoning_effort else {}

    def _serialize_tool(self, tool: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        }

    def _serialize_message(self, message: ConversationMessage) -> list[dict[str, Any]]:
        """Convert one canonical message into Chat Completions wire messages."""

        role = str(message.get("role") or "user")
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]

        if role == "user":
            payload_messages: list[dict[str, Any]] = []
            has_media = any(block.get("type") in {"image", "document"} for block in blocks)
            user_content: str | list[dict[str, Any]]
            if has_media:
                media_parts: list[dict[str, Any]] = []
                for block in blocks:
                    block_type = block.get("type")
                    if block_type == "text":
                        text = str(block.get("text") or "")
                        if text:
                            media_parts.append({"type": "text", "text": text})
                    elif block_type == "image":
                        mime_type, data = load_image_block_payload(block)
                        media_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{data}"},
                            }
                        )
                    elif block_type == "document":
                        mime_type, data, name = load_document_block_payload(block)
                        media_parts.append(
                            {
                                "type": "file",
                                "file": {
                                    "filename": name or "document.pdf",
                                    "file_data": f"data:{mime_type};base64,{data}",
                                },
                            }
                        )
                user_content = media_parts
            else:
                user_content = "\n".join(
                    str(block.get("text") or "")
                    for block in blocks
                    if block.get("type") == "text" and block.get("text")
                )

            if user_content:
                payload_messages.append({"role": "user", "content": user_content})

            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                payload_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id") or "",
                        "content": str(block.get("output") or ""),
                    }
                )
            return payload_messages

        if role != "assistant":
            return []

        text_parts = [
            str(block.get("text") or "") for block in blocks if block.get("type") == "text" and block.get("text")
        ]
        thinking_blocks = [block for block in blocks if block.get("type") == "thinking"]
        tool_use_blocks = [block for block in blocks if block.get("type") == "tool_use"]

        payload: dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts),
        }

        if tool_use_blocks:
            payload["tool_calls"] = [
                {
                    "id": block.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(
                            block.get("input") if isinstance(block.get("input"), dict) else {},
                            ensure_ascii=False,
                        ),
                    },
                }
                for block in tool_use_blocks
            ]

        if thinking_blocks:
            payload.update(self._serialize_reasoning(thinking_blocks))

        return [payload]

    def _serialize_reasoning(self, thinking_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Replay canonical thinking through the provider's reasoning field.

        When the source provider did not record a native field name, default to
        `reasoning_content`, which is the common reasoning slot used by the
        OpenAI-compatible thinking providers we support.
        """

        thinking_text = "\n".join(str(block.get("text") or "") for block in thinking_blocks if block.get("text"))
        native_meta = get_native_meta(thinking_blocks[0])
        reasoning_field = str(native_meta.get("reasoning_field") or "")
        if reasoning_field == "reasoning_details":
            return {"reasoning_details": native_meta.get("reasoning_details") or []}
        if reasoning_field == "reasoning":
            return {"reasoning": thinking_text or None}
        if reasoning_field == "reasoning_content":
            return {"reasoning_content": thinking_text or None}
        return {"reasoning_content": thinking_text} if thinking_text else {}

    def _consume_reasoning_delta(
        self,
        thinking_parts: list[str],
        native_meta: dict[str, Any],
        delta: Any,
    ) -> str:
        # Third-party providers surface reasoning through non-standard extras.
        # We check both the delta root and model_extra to cover both patterns.
        # Known fields: reasoning, reasoning_content, reasoning_details.
        values: dict[str, Any] = {}
        for source in (delta, getattr(delta, "model_extra", None) or {}):
            for field in ("reasoning", "reasoning_content", "reasoning_details"):
                if field in values:
                    continue
                if isinstance(source, dict):
                    if field not in source:
                        continue
                    values[field] = source[field]
                    continue
                value = getattr(source, field, None)
                if value is not None:
                    values[field] = value

        raw_details = dump_model(values.get("reasoning_details"))
        details = [item for item in raw_details if isinstance(item, dict)] if isinstance(raw_details, list) else None
        if details:
            stored_details = native_meta.setdefault("reasoning_details", [])
            stored_details.extend(details)
            native_meta["reasoning_field"] = "reasoning_details"
        elif native_meta.get("reasoning_field") != "reasoning_details":
            for field in ("reasoning", "reasoning_content"):
                if field in values:
                    native_meta["reasoning_field"] = field
                    break

        text = ""
        for field in ("reasoning", "reasoning_content"):
            value = values.get(field)
            if isinstance(value, str):
                text = value
                break
        if not text and details:
            text = "".join(str(item.get("text") or item.get("summary") or "") for item in details)

        if text:
            thinking_parts.append(text)
        return text


class DeepSeekAdapter(OpenAIChatAdapter):
    """DeepSeek's OpenAI-compatible chat endpoint.

    V4 supports both non-thinking and thinking modes. The shared "none" effort
    disables thinking; other explicit efforts enable thinking.
    """

    provider_id = "deepseek"
    label = "DeepSeek"
    default_base_url = "https://api.deepseek.com"
    env_api_key_names = ("DEEPSEEK_API_KEY",)
    default_models = ("deepseek-v4-pro", "deepseek-v4-flash")
    auto_discoverable = True
    supports_reasoning_effort = True

    @override
    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        effort = request.reasoning_effort
        if effort == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        if effort:
            return {
                "reasoning_effort": effort,
                "extra_body": {"thinking": {"type": "enabled"}},
            }
        return {}


class ZAIAdapter(OpenAIChatAdapter):
    """Z.AI's OpenAI-compatible chat endpoint.

    GLM models think by default. The explicit thinking parameter is sent only
    so ``clear_thinking=False`` preserves reasoning across multi-turn tool loops.
    """

    provider_id = "zai"
    label = "Z.AI"
    default_base_url = "https://api.z.ai/api/paas/v4/"
    env_api_key_names = ("ZAI_API_KEY",)
    default_models = ("glm-5.2",)
    auto_discoverable = True
    supports_reasoning_effort = True

    @override
    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {"extra_body": {"thinking": {"type": "enabled", "clear_thinking": False}}}
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        return payload


class XAIAdapter(OpenAIChatAdapter):
    """xAI's OpenAI-compatible Chat Completions endpoint."""

    provider_id = "xai"
    label = "xAI"
    default_base_url = "https://api.x.ai/v1"
    env_api_key_names = ("XAI_API_KEY",)
    default_models = ("grok-4.5",)
    auto_discoverable = True
    supports_reasoning_effort = True


class OpenRouterAdapter(OpenAIChatAdapter):
    """OpenRouter's OpenAI-compatible chat endpoint."""

    provider_id = "openrouter"
    label = "OpenRouter"
    default_base_url = "https://openrouter.ai/api/v1"
    env_api_key_names = ("OPENROUTER_API_KEY",)
    default_models = ("openrouter/auto",)
    auto_discoverable = True
    supports_reasoning_effort = True

    @override
    def _can_replay_native_history(self, message: ConversationMessage, request: ProviderRequest) -> bool:
        raw_meta = message.get("meta")
        return bool(isinstance(raw_meta, dict) and raw_meta.get("provider") == self.provider_id)

    @override
    def _project_incompatible_thinking(self, block: dict[str, Any]) -> dict[str, Any] | None:
        text = str(block.get("text") or "")
        return thinking_block(text) if text else None

    @override
    def _serialize_reasoning(self, thinking_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        if get_native_meta(thinking_blocks[0]):
            return super()._serialize_reasoning(thinking_blocks)

        thinking_text = "\n".join(str(block.get("text") or "") for block in thinking_blocks if block.get("text"))
        return {"reasoning": thinking_text} if thinking_text else {}

    @override
    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        if request.reasoning_effort:
            return {"extra_body": {"reasoning": {"effort": request.reasoning_effort}}}
        return {}
