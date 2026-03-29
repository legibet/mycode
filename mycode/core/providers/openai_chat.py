"""Chat Completions adapters for OpenAI-compatible providers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import APIError, AsyncOpenAI

from mycode.core.messages import assistant_message, text_block, thinking_block, tool_use_block
from mycode.core.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
    dump_model,
    get_native_meta,
)
from mycode.core.tools import parse_tool_arguments


@dataclass
class _ChatToolCallState:
    """Accumulate one streamed tool call from chat-completions deltas."""

    index: int
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

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        api_key = self.require_api_key(request.api_key)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.resolve_base_url(request.api_base),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

        # Keep the streamed turn state local to this adapter so the wire-format
        # mapping stays readable in one file.
        tool_calls: dict[int, _ChatToolCallState] = {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        thinking_native_meta: dict[str, Any] = {}
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
                    thinking_native_meta.update(reasoning_meta_update)
                    yield ProviderStreamEvent("thinking_delta", {"text": reasoning_delta})

                if delta.content:
                    text_parts.append(delta.content)
                    yield ProviderStreamEvent("text_delta", {"text": delta.content})

                for tool_call in delta.tool_calls or []:
                    index = tool_call.index or 0
                    state = tool_calls.setdefault(index, _ChatToolCallState(index=index))
                    if tool_call.id:
                        state.tool_id = tool_call.id
                    function = tool_call.function
                    if function is None:
                        continue
                    if function.name:
                        state.name = function.name
                    if function.arguments:
                        state.arguments_text += function.arguments
        except APIError as exc:
            raise ValueError(str(exc)) from exc

        blocks = []
        if thinking_parts:
            blocks.append(
                thinking_block(
                    "".join(thinking_parts),
                    meta={"native": thinking_native_meta} if thinking_native_meta else None,
                )
            )
        if text_parts:
            blocks.append(text_block("".join(text_parts)))

        for index in sorted(tool_calls):
            state = tool_calls[index]
            raw_arguments = state.arguments_text
            parsed_arguments = parse_tool_arguments(raw_arguments)
            if isinstance(parsed_arguments, str):
                tool_input = {}
                meta = {"native": {"raw_arguments": raw_arguments}}
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
            usage=dump_model(usage),
        )
        yield ProviderStreamEvent("message_done", {"message": final_message})

    def _build_request_payload(self, request: ProviderRequest) -> dict[str, Any]:
        prepared_messages = self.prepare_messages(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": self._build_messages(prepared_messages, system=request.system),
            "tools": [self._serialize_tool(tool) for tool in request.tools] or None,
            "tool_choice": "auto" if request.tools else None,
            "max_tokens": request.max_tokens,
            "stream_options": {"include_usage": True},
        }
        payload.update(self._build_provider_payload_overrides(request))
        return {key: value for key, value in payload.items() if value is not None}

    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        return {}

    def _build_messages(self, messages: list[dict[str, Any]], *, system: str) -> list[dict[str, Any]]:
        payload_messages = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        for message in messages:
            payload_messages.extend(self._serialize_message(message))
        return payload_messages

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
            text_parts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
            payload_messages = []
            if text_parts:
                payload_messages.append({"role": "user", "content": "\n".join(part for part in text_parts if part)})

            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                payload_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id") or "",
                        "content": str(block.get("model_text") or ""),
                    }
                )
            return payload_messages

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
        return {"reasoning_content": thinking_text} if thinking_text else {}

    def _extract_reasoning_delta(self, delta: Any) -> tuple[str, dict[str, Any]]:
        # Third-party providers surface reasoning through non-standard extras.
        # We check both the delta root and model_extra to cover both patterns.
        # Known fields: reasoning_content (Moonshot/MiniMax chat), reasoning_details (some others).
        for source in (delta, getattr(delta, "model_extra", None) or {}):
            if isinstance(source, dict):
                reasoning_content = source.get("reasoning_content")
                reasoning_details = source.get("reasoning_details")
            else:
                reasoning_content = getattr(source, "reasoning_content", None)
                reasoning_details = getattr(source, "reasoning_details", None)

            if isinstance(reasoning_content, str) and reasoning_content:
                return reasoning_content, {"reasoning_field": "reasoning_content"}

            if isinstance(reasoning_details, list) and reasoning_details:
                reasoning_text = "".join(
                    str(item.get("text") or "") for item in reasoning_details if isinstance(item, dict)
                )
                if reasoning_text:
                    return reasoning_text, {
                        "reasoning_field": "reasoning_details",
                        "reasoning_details": reasoning_details,
                    }

        return "", {}


class DeepSeekAdapter(OpenAIChatAdapter):
    """DeepSeek's OpenAI-compatible chat endpoint.

    deepseek-reasoner always thinks — no parameter needed to enable it.
    deepseek-chat does not think by default; send thinking: {"type": "enabled"}
    to activate it. We rely on the model's default behavior, so no overrides here.
    """

    provider_id = "deepseek"
    label = "DeepSeek"
    default_base_url = "https://api.deepseek.com"
    env_api_key_names = ("DEEPSEEK_API_KEY",)
    default_models = ("deepseek-chat", "deepseek-reasoner")
    auto_discoverable = True


class ZAIAdapter(OpenAIChatAdapter):
    """Z.AI's OpenAI-compatible chat endpoint.

    GLM models think by default. We still send the explicit thinking parameter
    so that clear_thinking=False preserves reasoning across multi-turn tool loops
    instead of resetting it on each turn.
    """

    provider_id = "zai"
    label = "Z.AI"
    default_base_url = "https://api.z.ai/api/paas/v4/"
    env_api_key_names = ("ZAI_API_KEY",)
    default_models = ("glm-5.1", "glm-5-turbo")
    auto_discoverable = True

    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        return {"extra_body": {"thinking": {"type": "enabled", "clear_thinking": False}}}


class OpenRouterAdapter(OpenAIChatAdapter):
    """OpenRouter's OpenAI-compatible chat endpoint."""

    provider_id = "openrouter"
    label = "OpenRouter"
    default_base_url = "https://openrouter.ai/api/v1"
    env_api_key_names = ("OPENROUTER_API_KEY",)
    default_models = ("openai/gpt-5.2", "anthropic/claude-sonnet-4.6")
    auto_discoverable = True
    supports_reasoning_effort = True

    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        if not request.reasoning_effort:
            return {}
        return {"extra_body": {"reasoning": {"effort": request.reasoning_effort}}}
