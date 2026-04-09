# Provider Adapters

All adapters live in `mycode-go/internal/provider/`. Each implements the `Adapter` interface from `base.go`.

## Interface

```go
type Adapter interface {
    Spec() Spec
    StreamTurn(ctx context.Context, req Request) <-chan StreamEvent
}
```

`prepareMessages()` in `base.go` converts canonical session history to provider-safe replay format. It handles:

- Stripping error, aborted, and cancelled assistant turns
- Projecting tool call ids when a provider restricts charset or length
- Replacing replay images with a short text notice when `request.SupportsImageInput` is false
- Replacing replay PDFs with a short text notice when `request.SupportsPDFInput` is false
- Flushing interrupted tool calls with synthetic error results

`StreamTurn()` yields normalized `StreamEvent` objects:

- `thinking_delta` — reasoning text
- `text_delta` — response text
- `message_done` — final canonical assistant message with all blocks and metadata
- `provider_error` — provider error

`Request` carries: provider, model, session_id, messages, system, tools, max_tokens, api_key, api_base, reasoning_effort, supports_image_input, supports_pdf_input.

## Adapters

### `anthropic` — `anthropic.go`

- SDK: `github.com/anthropics/anthropic-sdk-go` (official)
- API: Anthropic Messages API
- Base URL: `https://api.anthropic.com`
- API key env: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
- Default models: `claude-sonnet-4-6`, `claude-opus-4-6`
- `SupportsReasoningEffort`: true
- Adaptive thinking for `claude-sonnet-4-6` and `claude-opus-4-6`; manual `budget_tokens` for older reasoning models
- `reasoning_effort=xhigh` maps to `high` for sonnet-4-6, `max` for opus-4-6
- Adds ephemeral `cache_control` to system prompt block and the last user content block
- Tool call ids are projected to ASCII-safe format (letters, numbers, underscores, dashes, max 64 chars) with SHA1 collision suffix
- Images serialize as Anthropic `image` blocks with base64 `source`
- PDFs serialize as Anthropic `document` blocks with base64 `source`

### `moonshotai` — `anthropic.go`

- SDK: `github.com/anthropics/anthropic-sdk-go` against Moonshot's Anthropic-compatible endpoint
- Base URL: `https://api.moonshot.ai/anthropic`
- API key env: `MOONSHOT_API_KEY`
- Default models: `kimi-k2.5`
- `SupportsReasoningEffort`: true (maps to manual `budget_tokens`)
- Prior reasoning is replayed on later tool-loop turns when thinking is enabled
- Shares Anthropic-like ephemeral cache markers and tool call id projection
- Same image format as `anthropic`
- Same PDF format as `anthropic`

### `minimax` — `anthropic.go`

- SDK: `github.com/anthropics/anthropic-sdk-go` against MiniMax's Anthropic-compatible endpoint
- Base URL: `https://api.minimax.io/anthropic`
- API key env: `MINIMAX_API_KEY`
- Default models: `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`
- `SupportsReasoningEffort`: true (maps to manual `budget_tokens`)
- Preserves provider-native thinking signatures in `block.meta.native`
- Shares Anthropic-like ephemeral cache markers and tool call id projection
- Same image format as `anthropic`
- Same PDF format as `anthropic`

### `google` — `google.go`

- SDK: `google.golang.org/genai` (official)
- API: Gemini Developer API
- Base URL: `https://generativelanguage.googleapis.com`
- API key env: `GEMINI_API_KEY`, `GOOGLE_API_KEY`
- Default models: `gemini-3.1-pro-preview`, `gemini-3-flash-preview`
- `SupportsReasoningEffort`: true
- Reasoning effort mapping for Gemini 3:
  - `none` and `low` → `LOW` for `gemini-3.1-pro*`, `MINIMAL` for other `gemini-3*` models
  - `medium` → `MEDIUM`
  - `high` and `xhigh` → `HIGH`
- Replays `Part` metadata through `block.meta.native.part`, preserving function-call ids and thought signatures
- Cross-provider tool-loop fallback adds the documented dummy thought signature to avoid replay failures
- Empty-text streaming parts that carry thought signatures are still persisted
- Gemini validates function_call id and name match between function_call and function_response pairs
- `ThinkingConfig.IncludeThoughts` is always true; effort level controls `thinkingLevel`
- Images serialize as `inline_data`
- PDFs serialize as `inline_data`

### `openai` — `openai_responses.go`

- SDK: `github.com/openai/openai-go/v3` (official)
- API: OpenAI Responses API
- Base URL: `https://api.openai.com/v1`
- API key env: `OPENAI_API_KEY`
- Default models: `gpt-5.4`, `gpt-5.4-mini`
- `SupportsReasoningEffort`: true (`reasoning = {"effort": ...}`, values: `none`, `low`, `medium`, `high`, `xhigh`)
- Runs stateless: `store=false`, `include=["reasoning.encrypted_content"]`
- Streaming turns persist completed output items from `response.output_item.done` under `assistant.meta.native.output_items` and replay them directly
- Tool results replay as `function_call_output`; foreign thinking is never converted to OpenAI reasoning items
- Passes `prompt_cache_key` using the current session id
- Tool schemas use `strict: true` with nullable optional parameters
- Images serialize as `input_image`
- PDFs serialize as `input_file`

### `openai_chat` — `openai_chat.go`

- SDK: `github.com/openai/openai-go/v3` (official)
- API: OpenAI Chat Completions
- `SupportsReasoningEffort`: false on the base `openai_chat` adapter
- `AutoDiscoverable`: false
- Intended for third-party OpenAI-compatible providers when the Responses API is unavailable
- Preserves third-party reasoning extensions from SDK extras
- Sends `stream_options: {include_usage: true}`
- Images serialize as `image_url` parts with data URLs
- PDFs serialize as `file` parts with base64 data URLs

### `deepseek` — `openai_chat.go`

- SDK: `github.com/openai/openai-go/v3` against DeepSeek's OpenAI-compatible endpoint
- Base URL: `https://api.deepseek.com`
- API key env: `DEEPSEEK_API_KEY`
- Default models: `deepseek-chat`, `deepseek-reasoner`
- `SupportsReasoningEffort`: false; DeepSeek controls thinking natively
- `AutoDiscoverable`: true
- Stored reasoning content is replayed on later requests when the protocol supports it

### `zai` — `openai_chat.go`

- SDK: `github.com/openai/openai-go/v3` against Z.AI's OpenAI-compatible endpoint
- Base URL: `https://api.z.ai/api/paas/v4/`
- API key env: `ZAI_API_KEY`
- Default models: `glm-5.1`, `glm-5-turbo`
- `SupportsReasoningEffort`: false; thinking is enabled by default via `extra_body.thinking`
- `AutoDiscoverable`: true
- `clear_thinking=false` preserves reasoning across multi-turn tool loops

### `openrouter` — `openai_chat.go`

- SDK: `github.com/openai/openai-go/v3` against OpenRouter's OpenAI-compatible endpoint
- Base URL: `https://openrouter.ai/api/v1`
- API key env: `OPENROUTER_API_KEY`
- Default models: `openrouter/auto`
- `SupportsReasoningEffort`: true (forwarded through `extra_body.reasoning.effort`)
- `AutoDiscoverable`: true
- Same image format as `openai_chat`
- Same PDF format as `openai_chat`

## Reasoning Effort Mapping

| effort   | anthropic / moonshotai / minimax      | google (3.x)          | openai / openrouter |
| -------- | ------------------------------------- | --------------------- | ------------------- |
| `none`   | thinking disabled                     | `LOW` or `MINIMAL`    | `none`              |
| `low`    | low `budget_tokens`                   | `LOW` or `MINIMAL`    | `low`               |
| `medium` | medium `budget_tokens`                | `MEDIUM`              | `medium`            |
| `high`   | high `budget_tokens`                  | `HIGH`                | `high`              |
| `xhigh`  | `high` (sonnet) or `max` (opus)       | `HIGH`                | `xhigh`             |

Config-resolved `reasoning_effort` is only applied when both `adapter.SupportsReasoningEffort` and `model_metadata.supports_reasoning` are true.

## Message Replay

`prepareMessages()` in `base.go` handles canonical → provider replay projection:

1. Skip assistant messages with `stop_reason` in `{error, aborted, cancelled}`
2. Project tool call ids to provider-safe format when required
3. Preserve `block.meta.native` for provider-specific replay data (signatures, output items, part metadata)
4. Replace replay images with a short text notice when `request.SupportsImageInput` is false
5. Replace replay PDFs with a short text notice when `request.SupportsPDFInput` is false
6. Insert synthetic error tool results when pending tool calls would otherwise make replay invalid

Provider-specific replay logic lives inside each adapter's serialization methods. Examples:

- Gemini replays stored native `Part` metadata
- OpenAI Responses replays stored native output items
- Anthropic replays caller and signature metadata
