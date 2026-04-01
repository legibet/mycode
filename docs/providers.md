# Provider Adapters

All adapters live in `mycode/core/providers/`. Each implements `ProviderAdapter` from `base.py`.

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

`prepare_messages()` converts canonical session history to provider-safe wire format. The base implementation (`_project_messages_for_replay` in `base.py`) handles:
- Stripping error/aborted/cancelled assistant turns
- Projecting tool call IDs (some providers restrict charset/length)
- Dropping replay images when `request.supports_image_input` is false
- Flushing interrupted tool calls with synthetic error results

`stream_turn()` yields `ProviderStreamEvent` objects:
- `thinking_delta` — reasoning text
- `text_delta` — response text
- `message_done` — final `ConversationMessage` with all blocks and metadata

`ProviderRequest` carries: provider, model, session_id, messages, system, tools, max_tokens, api_key, api_base, reasoning_effort, supports_image_input.

## Adapters

### `anthropic` — `anthropic_like.py`

- SDK: `anthropic` (official)
- API: Anthropic Messages API
- Base URL: `https://api.anthropic.com`
- API key env: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
- Default models: `claude-sonnet-4-6`, `claude-opus-4-6`
- `supports_reasoning_effort`: true
- Adaptive thinking for `claude-sonnet-4-6` / `claude-opus-4-6`; manual `budget_tokens` for older reasoning models
- `reasoning_effort=xhigh` maps to `high` for sonnet-4-6, `max` for opus-4-6
- Adds ephemeral `cache_control` to system prompt block and last user content block
- Tool call IDs projected to ASCII-safe format (letters, numbers, underscores, dashes, max 64 chars) with SHA1 collision suffix
- Images serialize as Anthropic `image` blocks with base64 `source`

### `moonshotai` — `anthropic_like.py`

- SDK: `anthropic` against Moonshot's Anthropic-compatible endpoint
- Base URL: `https://api.moonshot.ai/anthropic`
- API key env: `MOONSHOT_API_KEY`
- Default models: `kimi-k2.5`
- `supports_reasoning_effort`: true (maps to manual `budget_tokens`)
- Prior reasoning must be replayed on later tool-loop turns when thinking is enabled
- Shares Anthropic-like ephemeral cache markers and tool call ID projection
- Same image format as `anthropic`

### `minimax` — `anthropic_like.py`

- SDK: `anthropic` against MiniMax's Anthropic-compatible endpoint
- Base URL: `https://api.minimax.io/anthropic`
- API key env: `MINIMAX_API_KEY`
- Default models: `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`
- `supports_reasoning_effort`: true (maps to manual `budget_tokens`)
- Preserves provider-native thinking signatures in `block.meta.native`
- Shares Anthropic-like ephemeral cache markers and tool call ID projection
- Same image format as `anthropic`

### `google` — `gemini.py`

- SDK: `google-genai` (official)
- API: Gemini Developer API
- Base URL: `https://generativelanguage.googleapis.com`
- API key env: `GEMINI_API_KEY`, `GOOGLE_API_KEY`
- Default models: `gemini-3.1-pro-preview`, `gemini-3-flash-preview`
- `supports_reasoning_effort`: true (Gemini 3 models only, via `thinking_level`)
- Reasoning effort mapping for Gemini 3:
  - `none`/`low` → `LOW` for `gemini-3.1-pro*`, `MINIMAL` for other `gemini-3*` models
  - `medium` → `MEDIUM`
  - `high`/`xhigh` → `HIGH`
- Replays `Part` metadata through `block.meta.native.part`, preserving function-call ids and thought signatures
- Cross-provider tool-loop fallback: adds documented dummy thought signature to avoid 400 errors
- Empty-text streaming parts that carry thought signatures must still be persisted
- Gemini validates function_call id/name match between function_call and function_response pairs
- `thinking_config.include_thoughts` always true; effort level controls `thinking_level`
- Images serialize as `inline_data`

### `openai` — `openai_responses.py`

- SDK: `openai` (official)
- API: OpenAI Responses API
- Base URL: `https://api.openai.com/v1`
- API key env: `OPENAI_API_KEY`
- Default models: `gpt-5.4`, `gpt-5.4-mini`
- `supports_reasoning_effort`: true (`reasoning = {"effort": ...}`, values: `none/low/medium/high/xhigh`)
- Runs stateless: `store=false`, `include=["reasoning.encrypted_content"]`
- Native `response.output` items persisted under `assistant.meta.native.output_items` and replayed directly
- Tool results replay as `function_call_output`; foreign thinking never converted to OpenAI reasoning items
- Passes `prompt_cache_key` using current session id
- Tool schemas use `strict: true` with nullable optional parameters
- Images serialize as `input_image`

### `openai_chat` — `openai_chat.py`

- SDK: `openai` (official)
- API: OpenAI Chat Completions
- `supports_reasoning_effort`: false
- `auto_discoverable`: false (base class only, not used directly)
- Intended for third-party OpenAI-compatible providers when Responses API is unavailable
- Preserves third-party reasoning extensions (`reasoning_content`, `reasoning_details`) from SDK extras
- Sends `stream_options: {include_usage: true}`
- Images serialize as `image_url` parts with data URLs

### `deepseek` — `openai_chat.py`

- SDK: `openai` against DeepSeek's OpenAI-compatible endpoint
- Base URL: `https://api.deepseek.com`
- API key env: `DEEPSEEK_API_KEY`
- Default models: `deepseek-chat`, `deepseek-reasoner`
- `supports_reasoning_effort`: false; DeepSeek controls thinking natively
- `auto_discoverable`: true
- Stored reasoning content replayed on later requests when the protocol supports it

### `zai` — `openai_chat.py`

- SDK: `openai` against Z.AI's OpenAI-compatible endpoint
- Base URL: `https://api.z.ai/api/paas/v4/`
- API key env: `ZAI_API_KEY`
- Default models: `glm-5.1`, `glm-5-turbo`
- `supports_reasoning_effort`: false; thinking enabled by default via `thinking: {type: "enabled", clear_thinking: false}`
- `auto_discoverable`: true
- `clear_thinking: false` preserves reasoning across multi-turn tool loops

### `openrouter` — `openai_chat.py`

- SDK: `openai` against OpenRouter's OpenAI-compatible endpoint
- Base URL: `https://openrouter.ai/api/v1`
- API key env: `OPENROUTER_API_KEY`
- Default models: `openrouter/auto`
- `supports_reasoning_effort`: true (forwarded through `extra_body.reasoning.effort`)
- `auto_discoverable`: true
- Same image format as `openai_chat`

## Reasoning Effort Mapping

| effort   | anthropic / moonshotai / minimax       | google (3.x)          | openai / openrouter |
| -------- | -------------------------------------- | --------------------- | ------------------- |
| `none`   | thinking disabled                      | `LOW`/`MINIMAL` level | `none`              |
| `low`    | low `budget_tokens`                    | `LOW`/`MINIMAL` level | `low`               |
| `medium` | medium `budget_tokens`                 | `MEDIUM` level        | `medium`            |
| `high`   | high `budget_tokens`                   | `HIGH` level          | `high`              |
| `xhigh`  | `high` (sonnet) / `max` (opus) effort  | `HIGH` level          | `xhigh`             |

Config-resolved `reasoning_effort` is only applied when both `adapter.supports_reasoning_effort` and `model_metadata.supports_reasoning` (from the bundled catalog) are true.

## Message Replay

`prepare_messages()` in the base class handles the canonical → provider wire format projection:

1. Skip assistant messages with `stop_reason` in `{error, aborted, cancelled}`
2. Project tool call IDs to provider-safe format (only Anthropic-like adapters override this)
3. Preserve `block.meta.native` for provider-specific replay data (signatures, output items, part metadata)
4. Drop replay images when `request.supports_image_input` is false
5. Insert synthetic error tool results when pending tool calls would otherwise make replay invalid

Provider-specific replay logic lives inside each adapter's serialization methods (e.g., Gemini's `_build_contents` replays native `Part` metadata, OpenAI's `_native_output_items` replays stored output items). These run after `prepare_messages()` produces the canonical replay transcript.
