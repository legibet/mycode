"""Chat Completions adapters for OpenAI-compatible providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from openai import APIError, AsyncOpenAI

from mycode.core.messages import assistant_message, text_block, thinking_block, tool_use_block
from mycode.core.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
    dump_model,
)
from mycode.core.tools import parse_tool_arguments


@dataclass
class _ChatToolCallState:
    index: int
    tool_id: str | None = None
    name: str = ""
    arguments_parts: list[str] = field(default_factory=list)


class OpenAIChatAdapter(ProviderAdapter):
    """Base adapter for Chat Completions style providers."""

    provider_id = "openai_chat"
    label = "OpenAI Chat Completions"
    default_base_url = "https://api.openai.com/v1"
    env_api_key_names = ("OPENAI_API_KEY",)
    auto_discoverable = False
    replay_reasoning_only_for_tool_continuations = False

    async def stream_turn(self, request: ProviderRequest):
        api_key = self.require_api_key(request.api_key)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.resolve_base_url(request.api_base),
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

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
                        state.arguments_parts.append(function.arguments)
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
            raw_arguments = "".join(state.arguments_parts)
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
        for index in range(len(messages)):
            payload_messages.extend(self._serialize_message(messages, index))
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

    def _serialize_message(self, messages: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
        message = messages[index]
        role = str(message.get("role") or "user")
        blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]

        if role == "user":
            tool_result_messages = [
                self._serialize_tool_result(block) for block in blocks if block.get("type") == "tool_result"
            ]
            text_parts = [str(block.get("text") or "") for block in blocks if block.get("type") == "text"]
            payload_messages = []
            if text_parts:
                payload_messages.append({"role": "user", "content": "\n".join(part for part in text_parts if part)})
            payload_messages.extend(tool_result_messages)
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
            payload["tool_calls"] = [self._serialize_tool_use(block) for block in tool_use_blocks]

        if thinking_blocks and self._should_replay_assistant_reasoning(messages, index):
            payload.update(self._serialize_reasoning(thinking_blocks))

        return [payload]

    def _should_replay_assistant_reasoning(self, messages: list[dict[str, Any]], index: int) -> bool:
        if not self.replay_reasoning_only_for_tool_continuations:
            return True

        return self._is_tool_continuation_turn(messages, index)

    def _is_tool_continuation_turn(self, messages: list[dict[str, Any]], index: int) -> bool:
        """Return whether the next user message only contains tool results.

        DeepSeek and Z.AI require assistant reasoning to be replayed during the
        same tool loop, but not when the user starts a fresh question.
        """

        next_index = index + 1
        if next_index >= len(messages):
            return False

        next_message = messages[next_index]
        if next_message.get("role") != "user":
            return False

        blocks = [block for block in next_message.get("content") or [] if isinstance(block, dict)]
        has_tool_result = any(block.get("type") == "tool_result" for block in blocks)
        has_text = any(block.get("type") == "text" and str(block.get("text") or "").strip() for block in blocks)
        return has_tool_result and not has_text

    def _extract_reasoning_text_from_details(self, reasoning_details: Any) -> str:
        if not isinstance(reasoning_details, list) or not reasoning_details:
            return ""
        return "".join(str(item.get("text") or "") for item in reasoning_details if isinstance(item, dict))

    def _serialize_reasoning(self, thinking_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        thinking_text = "\n".join(str(block.get("text") or "") for block in thinking_blocks if block.get("text"))
        raw_meta = thinking_blocks[0].get("meta")
        native_meta: dict[str, Any] = {}
        if isinstance(raw_meta, dict):
            candidate = raw_meta.get("native")
            if isinstance(candidate, dict):
                native_meta = dict(candidate)

        reasoning_field = str(native_meta.get("reasoning_field") or "")
        if reasoning_field == "reasoning_content":
            return {"reasoning_content": thinking_text}
        if reasoning_field == "reasoning_details":
            return {"reasoning_details": native_meta.get("reasoning_details") or []}
        return {}

    def _serialize_tool_result(self, block: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": block.get("tool_use_id") or "",
            "content": str(block.get("content") or ""),
        }

    def _serialize_tool_use(self, block: dict[str, Any]) -> dict[str, Any]:
        raw_input = block.get("input")
        tool_input: dict[str, Any] = {}
        if isinstance(raw_input, dict):
            tool_input = dict(raw_input)
        return {
            "id": block.get("id") or "",
            "type": "function",
            "function": {
                "name": block.get("name") or "",
                "arguments": json.dumps(tool_input, ensure_ascii=False),
            },
        }

    def _extract_reasoning_delta(self, delta: Any) -> tuple[str, dict[str, Any]]:
        reasoning_content = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning_content, str) and reasoning_content:
            return reasoning_content, {"reasoning_field": "reasoning_content"}

        reasoning_details = getattr(delta, "reasoning_details", None)
        reasoning_text = self._extract_reasoning_text_from_details(reasoning_details)
        if reasoning_text:
            return reasoning_text, {
                "reasoning_field": "reasoning_details",
                "reasoning_details": reasoning_details,
            }

        extras = getattr(delta, "model_extra", None) or {}
        reasoning_content = extras.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            return reasoning_content, {"reasoning_field": "reasoning_content"}

        reasoning_details = extras.get("reasoning_details")
        reasoning_text = self._extract_reasoning_text_from_details(reasoning_details)
        if reasoning_text:
            return reasoning_text, {
                "reasoning_field": "reasoning_details",
                "reasoning_details": reasoning_details,
            }

        return "", {}


class DeepSeekAdapter(OpenAIChatAdapter):
    """DeepSeek's OpenAI-compatible chat endpoint."""

    provider_id = "deepseek"
    label = "DeepSeek"
    default_base_url = "https://api.deepseek.com"
    env_api_key_names = ("DEEPSEEK_API_KEY",)
    default_models = ("deepseek-chat", "deepseek-reasoner")
    auto_discoverable = True
    replay_reasoning_only_for_tool_continuations = True


class ZAIAdapter(OpenAIChatAdapter):
    """Z.AI's OpenAI-compatible chat endpoint."""

    provider_id = "zai"
    label = "Z.AI"
    default_base_url = "https://api.z.ai/api/paas/v4/"
    env_api_key_names = ("ZAI_API_KEY",)
    default_models = ("glm-5", "glm-4.7")
    auto_discoverable = True
    replay_reasoning_only_for_tool_continuations = True


class OpenRouterAdapter(OpenAIChatAdapter):
    """OpenRouter's OpenAI-compatible chat endpoint."""

    provider_id = "openrouter"
    label = "OpenRouter"
    default_base_url = "https://openrouter.ai/api/v1"
    env_api_key_names = ("OPENROUTER_API_KEY",)
    default_models = ("openai/gpt-5.2", "anthropic/claude-sonnet-4.6")
    auto_discoverable = True
    supports_reasoning_effort = True
    replay_reasoning_only_for_tool_continuations = True

    def _build_provider_payload_overrides(self, request: ProviderRequest) -> dict[str, Any]:
        if not request.reasoning_effort:
            return {}
        return {"extra_body": {"reasoning": {"effort": request.reasoning_effort}}}
