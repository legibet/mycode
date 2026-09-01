# Provider Adapters

All adapters live in `mycode/src/mycode/providers/`. Each implements `ProviderAdapter` from `base.py`.

## Interface

```python
class ProviderAdapter(ABC):
    provider_id: str
    label: str
    env_api_key_names: tuple[str, ...]
    default_models: tuple[str, ...]
    auto_discoverable: bool              # can be found from env alone
    supports_reasoning_effort: bool

    def stream_turn(request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]: ...
    def prepare_messages(request: ProviderRequest) -> list[ConversationMessage]: ...
    def project_tool_call_id(tool_call_id: str, used_tool_call_ids: set[str]) -> str: ...
```

`prepare_messages()` converts canonical session history to provider-safe wire format. The base implementation in `base.py` handles:

- Stripping error/cancelled assistant turns
- Projecting tool call IDs (some providers restrict charset/length)
- Replacing replay images with a short text notice when `request.supports_image_input` is false
- Replacing replay PDFs with a short text notice when `request.supports_pdf_input` is false
- Flushing interrupted tool calls with synthetic error results

`stream_turn()` yields `ProviderStreamEvent` objects:

- `stream_started` — emitted once, on the first event or chunk exposed by the upstream SDK; stops the Agent's `stream_start_timeout` timer and never reaches SDK consumers. A `message_done` without a prior marker also counts as stream start.
- `thinking_delta` — reasoning text
- `text_delta` — response text
- `message_done` — final `ConversationMessage` with all blocks and metadata

`ProviderRequest` carries: provider, model, session_id, messages, system, tools, max_tokens, api_key, api_base, reasoning_effort, supports_image_input, supports_pdf_input, request_timeout.

## Timeouts, Retries, and Errors

Retries are owned by the Agent runtime, so provider SDK retries are disabled: openai and anthropic clients are constructed with `max_retries=0`, gemini with an explicit `HttpRetryOptions(attempts=1)` (google-genai defaults to no retries, but flips to multiple attempts once `retry_options` is set at all).

`ProviderRequest.request_timeout` is the per-attempt transport timeout. openai and anthropic clients receive `httpx2.Timeout(request_timeout, connect=5)`, keeping the SDKs' 5s connect default. google-genai only takes a scalar per-request timeout (`HttpOptions.timeout`, milliseconds) that overrides any client-level `httpx2.Timeout`, so gemini has no separate connect phase; its pre-stream waits are bounded by the Agent's `stream_start_timeout` instead. Gemini receives application-owned sync and async HTTPX2 clients whose contexts own connection cleanup.

Adapters raise `ProviderError` for upstream failures. The shared normalizer handles transport errors and HTTP statuses; adapters classify transient errors carried inside successful streams. The Agent only reads `ProviderError.retryable`.

## Usage Normalization

Every adapter maps wire usage to canonical `meta.usage`. Field semantics and mappings are documented in docs/sessions.md. Missing fields remain unknown unless the protocol defines absence as zero.

Provider quirks:

- Gemini proto3 omits zero-valued counts, so absent optional counts become 0 after a usage payload arrives.
- DeepSeek: `prompt_tokens_details.cached_tokens` wins; top-level `prompt_cache_hit_tokens` is the fallback.
- OpenRouter: `usage.cost` is persisted as `meta.cost = {"total": ...}`. Other Chat Completions providers' `cost` extensions are ignored.
- Anthropic-compatible providers need all input and cache counters to compute effective input.

## Adapters

### `anthropic` — `anthropic_like.py`

- SDK: `anthropic` (official)
- API: Anthropic Messages API
- Base URL: `https://api.anthropic.com`
- API key env: `ANTHROPIC_API_KEY`
- Default models: `claude-sonnet-5`, `claude-opus-5`
- `supports_reasoning_effort`: true
- Default-on Claude 5 models and explicitly enabled thinking use adaptive summarized output; `none` disables thinking
- Sends other explicit values unchanged through `output_config.effort`
- Omits `temperature`; Anthropic-compatible providers use provider-default sampling
- Replays same-model native `thinking` and `redacted_thinking` unchanged; legacy signature-only blocks remain supported
- Adds ephemeral `cache_control` to system prompt block and last user content block
- Tool call IDs projected to ASCII-safe format (letters, numbers, underscores, dashes, max 64 chars) with SHA1 collision suffix
- Images serialize as Anthropic `image` blocks with base64 `source`
- PDFs serialize as Anthropic `document` blocks with base64 `source`

### `moonshotai` — `anthropic_like.py`

- SDK: `anthropic` against Moonshot's Anthropic-compatible endpoint
- Base URL: `https://api.moonshot.ai/anthropic`
- API key env: `MOONSHOT_API_KEY`
- Default models: `kimi-k3`, `kimi-k2.6`
- `supports_reasoning_effort`: true; `none` disables thinking, while other explicit values use adaptive thinking and pass unchanged through `output_config.effort`
- Omits `temperature`; Anthropic-compatible providers use provider-default sampling
- Replays native thinking blocks unchanged across tool loops
- Shares Anthropic-like ephemeral cache markers and tool call ID projection
- Same image format as `anthropic`
- Same PDF format as `anthropic`

### `minimax` — `anthropic_like.py`

- SDK: `anthropic` against MiniMax's Anthropic-compatible endpoint
- Base URL: `https://api.minimax.io/anthropic`
- API key env: `MINIMAX_API_KEY`
- Default models: `MiniMax-M3`
- `supports_reasoning_effort`: false
- `MiniMax-M3` uses adaptive thinking by default; MiniMax Anthropic endpoint does not support effort depth
- Omits `temperature`; Anthropic-compatible providers use provider-default sampling
- Replays native thinking blocks unchanged across tool loops
- Shares Anthropic-like ephemeral cache markers and tool call ID projection
- Same image format as `anthropic`
- Same PDF format as `anthropic`

### `google` — `gemini.py`

- SDK: `google-genai` (official)
- API: Gemini Developer API
- Base URL: `https://generativelanguage.googleapis.com`
- API key env: `GEMINI_API_KEY`, `GOOGLE_API_KEY`
- Default models: `gemini-3.7-flash`, `gemini-3.1-pro-preview`
- `supports_reasoning_effort`: true; explicit values are converted to Gemini's `ThinkingLevel` enum
- Replays original parts with their function-call ids and thought signatures
- Cross-provider tool-loop fallback: adds documented dummy thought signature to avoid 400 errors
- Empty-text streaming parts that carry thought signatures must still be persisted
- Gemini validates function_call id/name match between function_call and function_response pairs
- `thinking_config.include_thoughts` always true; explicit effort controls `thinking_level`
- Images serialize as `inline_data`
- PDFs serialize as `inline_data`

### `google_vertex` — `gemini.py`

- SDK: `google-genai>=2.20.0` (official; custom HTTPX2 clients are injected through `HttpOptions`)
- API: Google Agent Platform (formerly Vertex AI), selected with `enterprise=True`
- Base URL: built by the SDK from the auth mode and location; an explicit `api_base` overrides it
- API key env: `GOOGLE_CLOUD_API_KEY`
- ADC env: `GOOGLE_CLOUD_PROJECT` (required) and `GOOGLE_CLOUD_LOCATION` (optional; the SDK defaults ADC requests to `global`)
- Auth values are resolved independently and passed explicitly: key alone selects express mode; project/location alone uses Application Default Credentials; key + project/location uses the project-bound combined mode and skips ADC
- Combined key + project without location builds an invalid `locations/None` resource path (google-genai 2.20.0); set `GOOGLE_CLOUD_LOCATION` whenever combining them
- Never uses the SDK's implicit key discovery, so `GEMINI_API_KEY`/`GOOGLE_API_KEY` cannot leak from the `google` provider
- ADC is opt-in via a configured entry such as `{"type": "google_vertex"}`; `GOOGLE_CLOUD_PROJECT` alone does not auto-discover the provider (see `docs/config.md`)
- Default models, model metadata, reasoning effort, message projection, streaming, thought signatures, and image/PDF input are all inherited from `google`

### `openai` — `openai_responses.py`

- SDK: `openai` (official)
- API: OpenAI Responses API
- Base URL: `https://api.openai.com/v1`
- API key env: `OPENAI_API_KEY`
- Default models: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`
- `supports_reasoning_effort`: true (sent through `reasoning.effort`)
- OpenAI recommends Responses API for GPT-5.6 reasoning/tool-calling/multi-turn use cases; GPT-5.6 defaults to `medium` effort and `low` is the recommended first step for latency-sensitive workloads
- Runs stateless: `store=false`, `include=["reasoning.encrypted_content"]`
- When reasoning is enabled, requests `reasoning.summary=auto`; streams `response.reasoning_summary_text.delta` as canonical thinking and does not surface raw `response.reasoning_text.delta`
- Final reasoning items use `summary` text for the canonical thinking block and retain full native output items for replay
- OpenAI recommends reserving at least ~25k `max_output_tokens` for reasoning + output when first tuning reasoning models to avoid incomplete responses during reasoning
- Replays complete native output items for the same model; a `function_call` item marked `invalid_input` replays with `arguments: "{}"`
- Tool results replay as `function_call_output`
- Passes `prompt_cache_key` using current session id
- Tool schemas use `strict: true` with nullable optional parameters
- Images serialize as `input_image`
- PDFs serialize as `input_file`

### `openai_chat` — `openai_chat.py`

- SDK: `openai` (official)
- API: OpenAI Chat Completions
- `supports_reasoning_effort`: false by default; set it `true` in config (see `docs/config.md`) for endpoints that accept the standard top-level `reasoning_effort`
- Forwards any explicit effort through the standard top-level `reasoning_effort`; `None` leaves it unset
- `auto_discoverable`: false (base class only, not used directly)
- Intended for third-party OpenAI-compatible providers when Responses API is unavailable
- Preserves same-model reasoning extensions from SDK extras:
  - `reasoning` replays as `reasoning`
  - `reasoning_content` replays as `reasoning_content`, including empty field markers
  - `reasoning_details` replays as `reasoning_details`
- Empty provider-native reasoning blocks are retained for replay even when no reasoning text was shown to the user
- Sends `stream_options: {include_usage: true}`
- Images serialize as `image_url` parts with data URLs
- PDFs serialize as `file` parts with base64 data URLs

### `deepseek` — `openai_chat.py`

- SDK: `openai` against DeepSeek's OpenAI-compatible endpoint
- Base URL: `https://api.deepseek.com`
- API key env: `DEEPSEEK_API_KEY`
- Default models: `deepseek-v4-pro`, `deepseek-v4-flash`
- `supports_reasoning_effort`: true; `none` sends `thinking: {type: "disabled"}`, while other explicit values pass through unchanged with thinking enabled
- `auto_discoverable`: true
- Same-model `reasoning_content` is replayed on later requests, including empty markers after tool turns

### `zai` — `openai_chat.py`

- SDK: `openai` against Z.AI's OpenAI-compatible endpoint
- Base URL: `https://api.z.ai/api/paas/v4/`
- API key env: `ZAI_API_KEY`
- Default models: `glm-5.3`, `glm-5.3-flash`
- `supports_reasoning_effort`: true; thinking enabled by default via `thinking: {type: "enabled", clear_thinking: false}`; explicit values pass through unchanged
- `auto_discoverable`: true
- `clear_thinking: false` preserves same-model reasoning across tool loops; historical `reasoning_content` is replayed unmodified

### `openrouter` — `openai_chat.py`

- SDK: `openai` against OpenRouter's OpenAI-compatible endpoint
- Base URL: `https://openrouter.ai/api/v1`
- API key env: `OPENROUTER_API_KEY`
- Default models: `openrouter/auto`
- `supports_reasoning_effort`: true (forwarded through `extra_body.reasoning.effort`)
- `auto_discoverable`: true
- Replays `reasoning_details` unchanged across OpenRouter models
- Accepts `reasoning` and `reasoning_content`; foreign readable thinking uses `reasoning`
- Same image format as `openai_chat`
- Same PDF format as `openai_chat`

### `alibaba` — `openai_chat.py`

- SDK: `openai` against Alibaba Cloud Model Studio Chat Completions
- Base URL: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- API key env: `DASHSCOPE_API_KEY`
- Default models: `qwen3.8-max`, `qwen3.8-flash`
- `supports_reasoning_effort`: true; explicit values pass through unchanged
- Always sends `preserve_thinking: true`
- Uses `max_completion_tokens` instead of `max_tokens`

### `xai` — `openai_chat.py`

- SDK: `openai` against xAI's OpenAI-compatible Chat Completions endpoint
- Base URL: `https://api.x.ai/v1`
- API key env: `XAI_API_KEY`
- Default models: `grok-4.6`
- `supports_reasoning_effort`: true; explicit values pass through the standard top-level `reasoning_effort`
- `auto_discoverable`: true
- Grok reasoning streams as `reasoning_content`, replayed for the same model by the shared `openai_chat` handling
- Same image format as `openai_chat`
- Same PDF format as `openai_chat`

## Stop Reason Normalization

Adapters persist only these canonical values: `stop`, `tool_use`, `length`, `error`, and `unknown` (the Agent adds `cancelled` for user cancellation).

| adapter | provider values |
| --- | --- |
| Anthropic-like | `end_turn`, `stop_sequence` -> `stop`; `tool_use` -> `tool_use`; `max_tokens` -> `length` |
| OpenAI Chat | `stop` -> `stop`; `tool_calls`, `function_call` -> `tool_use`; `length` -> `length`; `content_filter` -> `error` |
| Gemini | `STOP` -> `stop`; `MAX_TOKENS` -> `length`; documented safety, policy, recitation, and malformed tool-call reasons -> `error` |
| OpenAI Responses | `completed` -> `stop` or `tool_use` from output items; `incomplete.max_output_tokens` -> `length`; `incomplete.content_filter` and `failed` -> `error` |

An adapter returns `unknown` for a provider value outside its documented mapping. Raw provider values are not stored in assistant metadata.

## Message Replay

Before serialization, replay history is normalized:

1. Skip assistant messages with `stop_reason` in `{error, cancelled}`
2. Project tool call IDs to provider-safe format (only Anthropic-like adapters override this)
3. Preserve native reasoning data only when the source provider and selected model match the target
4. Replay readable foreign or different-model thinking as assistant text and drop opaque native state
5. Replace unsupported replay images and PDFs with short text notices
6. Insert synthetic error tool results when pending tool calls would otherwise make replay invalid

OpenRouter is the documented exception: its `reasoning_details` remains portable across models within OpenRouter, and readable foreign thinking uses OpenRouter's normalized `reasoning` field.

For OpenAI-compatible chat providers, empty `thinking` blocks with `block.meta.native` are intentionally preserved. Some providers require a reasoning field to be returned even when its value is empty or null.

Upstream behavior references:

- Anthropic [thinking](https://platform.claude.com/docs/en/build-with-claude/thinking): replay native blocks unchanged and strip them when switching models.
- Gemini [thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures): replay signatures in their original parts; use the documented dummy signature for transferred tool traces.
- OpenAI [reasoning](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-without-stored-responses): stateless Responses requests replay the complete native output items.
- OpenAI Chat/SDK: additive response fields are allowed by the [API compatibility policy](https://developers.openai.com/api/reference/overview#backwards-compatibility), and the official Python SDK exposes undocumented response properties through [`model_extra`](https://github.com/openai/openai-python#undocumented-request-params).
- OpenRouter: [reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning) can be returned and replayed as `reasoning`, `reasoning_content`, or structured `reasoning_details`; streamed `reasoning_details` chunks must be concatenated in order and replayed unmodified.
- Z.AI: [preserved/interleaved thinking](https://docs.z.ai/guides/capabilities/thinking-mode#preserved-thinking) requires returning historical `reasoning_content` unmodified when `clear_thinking: false` is used.
