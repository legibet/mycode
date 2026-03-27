"""Google Gemini adapter built on the official google-genai Python SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from google import genai
from google.genai import types
from google.genai.errors import APIError

from mycode.core.messages import assistant_message, text_block, thinking_block, tool_use_block
from mycode.core.providers.base import (
    DEFAULT_REQUEST_TIMEOUT,
    ProviderAdapter,
    ProviderRequest,
    ProviderStreamEvent,
    get_native_meta,
)

_DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


def _to_json(value: Any) -> Any:
    """Convert SDK objects into JSON-safe plain data."""

    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return _to_json(value.model_dump())
    if isinstance(value, list):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: normalized for key, item in value.items() if (normalized := _to_json(item)) is not None}
    return value


class GoogleGeminiAdapter(ProviderAdapter):
    """Adapter for the Gemini Developer API."""

    provider_id = "google"
    label = "Google Gemini"
    default_base_url = "https://generativelanguage.googleapis.com"
    env_api_key_names = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    default_models = ("gemini-3.1-pro-preview", "gemini-3-flash-preview")
    supports_reasoning_effort = True

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]:
        api_key = self.require_api_key(request.api_key)
        client = genai.Client(api_key=api_key, http_options=self._http_options(request.api_base))

        blocks: list[dict[str, Any]] = []
        response_id: str | None = None
        response_model: str | None = None
        finish_reason: str | None = None
        finish_message: str | None = None
        usage: dict[str, Any] | None = None

        try:
            stream = await client.aio.models.generate_content_stream(
                model=request.model,
                contents=self._build_contents(request),
                config=self._build_config(request),
            )
            async for chunk in stream:
                response_id = response_id or getattr(chunk, "response_id", None)
                response_model = response_model or getattr(chunk, "model_version", None)
                usage = _to_json(getattr(chunk, "usage_metadata", None)) or usage

                candidates = getattr(chunk, "candidates", None) or []
                if not candidates:
                    continue
                candidate = candidates[0]

                finish_reason = _to_json(getattr(candidate, "finish_reason", None)) or finish_reason
                finish_message = getattr(candidate, "finish_message", None) or finish_message

                for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
                    for event in self._consume_part(blocks, part):
                        yield event
        except APIError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            try:
                await client.aio.aclose()
            except Exception:
                pass

        yield ProviderStreamEvent(
            "message_done",
            {
                "message": assistant_message(
                    blocks,
                    provider=self.provider_id,
                    model=response_model or request.model,
                    provider_message_id=response_id,
                    stop_reason=str(finish_reason) if finish_reason else None,
                    usage=usage,
                    native_meta={"finish_message": str(finish_message)} if finish_message else None,
                )
            },
        )

    def _http_options(self, api_base: str | None) -> types.HttpOptions:
        base_url = self.resolve_base_url(api_base)
        api_version = "v1beta"
        if base_url and urlparse(base_url).path.rstrip("/").lower().endswith(("/v1", "/v1beta")):
            api_version = None
        return types.HttpOptions(base_url=base_url, api_version=api_version, timeout=int(DEFAULT_REQUEST_TIMEOUT))

    def _build_contents(self, request: ProviderRequest) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}

        for message in self.prepare_messages(request):
            role = str(message.get("role") or "")
            blocks = [block for block in message.get("content") or [] if isinstance(block, dict)]

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                needs_dummy_signature = True
                for block in blocks:
                    if block.get("type") == "tool_use":
                        tool_id = str(block.get("id") or "")
                        tool_name = str(block.get("name") or "")
                        if tool_id and tool_name:
                            tool_names[tool_id] = tool_name

                    native_part = get_native_meta(block).get("part")
                    if isinstance(native_part, dict):
                        parts.append(dict(native_part))
                        if native_part.get("function_call") and native_part.get("thought_signature"):
                            needs_dummy_signature = False
                        continue

                    block_type = block.get("type")
                    if block_type == "thinking":
                        parts.append({"text": str(block.get("text") or ""), "thought": True})
                    elif block_type == "text":
                        parts.append({"text": str(block.get("text") or "")})
                    elif block_type == "tool_use":
                        part: dict[str, Any] = {
                            "function_call": {
                                "id": block.get("id") or "",
                                "name": block.get("name") or "",
                                "args": block.get("input") if isinstance(block.get("input"), dict) else {},
                            }
                        }
                        # Gemini 3 validates the first function call in each step of
                        # the current turn. When history came from another provider,
                        # there is no real thought signature to replay, so we use the
                        # official dummy signature to keep cross-provider tool loops
                        # working.
                        if needs_dummy_signature:
                            part["thought_signature"] = _DUMMY_THOUGHT_SIGNATURE
                            needs_dummy_signature = False
                        parts.append(part)

                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if role != "user":
                continue

            parts: list[dict[str, Any]] = []
            for block in blocks:
                block_type = block.get("type")
                if block_type == "text":
                    parts.append({"text": str(block.get("text") or "")})
                    continue
                if block_type != "tool_result":
                    continue

                tool_id = str(block.get("tool_use_id") or "")
                response: dict[str, Any] = {"result": str(block.get("model_text") or "")}
                if block.get("is_error"):
                    response["is_error"] = True
                parts.append(
                    {
                        "function_response": {
                            "id": tool_id,
                            "name": tool_names.get(tool_id, ""),
                            "response": response,
                        }
                    }
                )

            if parts:
                contents.append({"role": "user", "parts": parts})

        return contents

    def _build_config(self, request: ProviderRequest) -> types.GenerateContentConfig:
        tools = None
        tool_config = None
        automatic_function_calling = None
        if request.tools:
            tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=str(tool.get("name") or ""),
                            description=str(tool.get("description") or ""),
                            parameters_json_schema=tool.get("input_schema") or {"type": "object", "properties": {}},
                        )
                        for tool in request.tools
                    ]
                )
            ]
            automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
            tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(stream_function_call_arguments=False)
            )

        thinking_config = types.ThinkingConfig(include_thoughts=True)
        if request.reasoning_effort and request.model.lower().startswith("gemini-3"):
            # Official OpenAI-compat mapping:
            # Gemini 3.1 Pro: minimal -> low
            # Gemini 3 Flash:   minimal -> minimal
            effort = request.reasoning_effort
            if effort in {"none", "low"}:
                thinking_config.thinking_level = (
                    types.ThinkingLevel.LOW
                    if request.model.lower().startswith("gemini-3.1-pro")
                    else types.ThinkingLevel.MINIMAL
                )
            elif effort == "medium":
                thinking_config.thinking_level = types.ThinkingLevel.MEDIUM
            else:
                thinking_config.thinking_level = types.ThinkingLevel.HIGH

        return types.GenerateContentConfig(
            system_instruction=request.system or None,
            max_output_tokens=request.max_tokens,
            tools=tools,
            tool_config=tool_config,
            automatic_function_calling=automatic_function_calling,
            thinking_config=thinking_config,
        )

    def _consume_part(self, blocks: list[dict[str, Any]], part: Any) -> list[ProviderStreamEvent]:
        native_part = _to_json(part) or {}
        if native_part.get("thought") is False:
            native_part.pop("thought", None)

        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            tool_input = getattr(function_call, "args", None)
            blocks.append(
                tool_use_block(
                    tool_id=str(getattr(function_call, "id", None) or f"tool_call_{len(blocks)}"),
                    name=str(getattr(function_call, "name", None) or ""),
                    input=tool_input if isinstance(tool_input, dict) else {},
                    meta={"native": {"part": native_part}},
                )
            )
            return []

        text = getattr(part, "text", None)
        if text is None or text == "":
            if not native_part.get("thought_signature"):
                return []

            # Gemini may put the final thought signature into an empty-text part.
            # Keep it as a separate empty block so replay preserves the original
            # part boundary instead of merging the signature into another block.
            blocks.append(
                thinking_block("", meta={"native": {"part": native_part}})
                if bool(getattr(part, "thought", False))
                else text_block("", meta={"native": {"part": native_part}})
            )
            return []

        is_thought = bool(getattr(part, "thought", False))
        event = ProviderStreamEvent("thinking_delta" if is_thought else "text_delta", {"text": str(text)})
        block_type = "thinking" if is_thought else "text"

        # Gemini may stream one logical thought/text across many chunks.
        # Merge only when the block kind matches and we are not combining
        # distinct thought signatures.
        if blocks and blocks[-1].get("type") == block_type:
            last_part = get_native_meta(blocks[-1]).get("part")
            if isinstance(last_part, dict):
                last_signature = last_part.get("thought_signature")
                current_signature = native_part.get("thought_signature")
                if not (last_signature and current_signature and last_signature != current_signature):
                    blocks[-1]["text"] = f"{blocks[-1].get('text') or ''}{text}"
                    last_part["text"] = f"{last_part.get('text') or ''}{text}"
                    if current_signature and not last_signature:
                        last_part["thought_signature"] = current_signature
                    return [event]

        block = (
            thinking_block(str(text), meta={"native": {"part": native_part}})
            if is_thought
            else text_block(str(text), meta={"native": {"part": native_part}})
        )
        blocks.append(block)
        return [event]
