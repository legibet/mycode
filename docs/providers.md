# Provider Adapters

All adapters live in `mycode/core/providers/`. Each implements `ProviderAdapter` from `base.py`.

## Interface

```python
class ProviderAdapter(ABC):
    provider_id: str
    label: str
    env_api_key_names: tuple[str, ...]   # env vars checked for API key auto-discovery
    default_models: tuple[str, ...]
    auto_discoverable: bool              # can be found from env alone
    supports_reasoning_effort: bool

    def stream_turn(request: ProviderRequest) -> AsyncIterator[ProviderStreamEvent]: ...
    def prepare_messages(request: ProviderRequest) -> list[ConversationMessage]: ...
    def project_tool_call_id(tool_call_id: str, used_tool_call_ids: set[str]) -> str: ...  # override if provider restricts id format
```

`prepare_messages()` converts canonical session history to provider-safe wire format. This is where provider-specific replay logic lives (thinking replay, native output items, etc.).

## Adapters

### `anthropic` — `anthropic_like.py`

- SDK: `anthropic` (official)
- API: Anthropic Messages API
- Base URL: `https://api.anthropic.com`
- API key env: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
- `supports_reasoning_effort`: true
- Adaptive thinking for `claude-sonnet-4-6` / `claude-opus-4-6`; manual `budget_tokens` for older reasoning models
- `reasoning_effort=xhigh` maps to `high` for sonnet-4-6, `max` for opus-4-6
- Adds ephemeral `cache_control` to system prompt block and last user content block

### `moonshotai` — `anthropic_like.py`

- SDK: `anthropic` against Moonshot's Anthropic-compatible endpoint
- Base URL: `https://api.moonshot.ai/anthropic`
- API key env: `MOONSHOT_API_KEY`
- `supports_reasoning_effort`: true (maps to manual `budget_tokens`)
- Prior reasoning must be replayed on later tool-loop turns when thinking is enabled
- Shares Anthropic-like ephemeral cache markers

### `minimax` — `anthropic_like.py`

- SDK: `anthropic` against MiniMax's Anthropic-compatible endpoint
- Base URL: `https://api.minimax.io/anthropic`
- API key env: `MINIMAX_API_KEY`
- `supports_reasoning_effort`: true (maps to manual `budget_tokens`)
- Preserves provider-native thinking signatures in `block.meta.native`
- Shares Anthropic-like ephemeral cache markers

### `google` — `gemini.py`

- SDK: `google-genai` (official)
- API: Gemini Developer API
- Base URL: `https://generativelanguage.googleapis.com`
- API key env: `GEMINI_API_KEY`, `GOOGLE_API_KEY`
- Default models: `gemini-3.1-pro-preview`, `gemini-3-flash-preview`
- `supports_reasoning_effort`: true (Gemini 3 models only, via `thinking_level`)
- Replays `Part` metadata through `block.meta.native.part`, preserving function-call ids and thought signatures
- Cross-provider tool-loop fallback: adds documented dummy thought signature to avoid 400 errors
- Empty-text streaming parts that carry thought signatures must still be persisted

### `openai` — `openai_responses.py`

- SDK: `openai` (official)
- API: OpenAI Responses API
- Base URL: `https://api.openai.com/v1`
- API key env: `OPENAI_API_KEY`
- `supports_reasoning_effort`: true (`reasoning = {"effort": ...}`, values: `none/low/medium/high/xhigh`)
- Runs stateless: `store=false`, `include=["reasoning.encrypted_content"]`
- Native `response.output` items persisted under `assistant.meta.native.output_items` and replayed directly
- Tool results replay as `function_call_output`; foreign thinking never converted to OpenAI reasoning items
- Passes `prompt_cache_key` using current session id

### `openai_chat` — `openai_chat.py`

- SDK: `openai` (official)
- API: OpenAI Chat Completions
- `supports_reasoning_effort`: false
- Intended for third-party OpenAI-compatible providers when Responses API is unavailable
- Preserves third-party reasoning extensions (`reasoning_content`, `reasoning_details`) from SDK extras

### `deepseek` — `openai_chat.py`

- SDK: `openai` against DeepSeek's OpenAI-compatible endpoint
- Base URL: `https://api.deepseek.com`
- API key env: `DEEPSEEK_API_KEY`
- Default models: `deepseek-chat`, `deepseek-reasoner`
- `supports_reasoning_effort`: false; DeepSeek controls thinking natively
- Stored reasoning content replayed on later requests when the protocol supports it

### `zai` — `openai_chat.py`

- SDK: `openai` against Z.AI's OpenAI-compatible endpoint
- Base URL: `https://api.z.ai/api/paas/v4/`
- API key env: `ZAI_API_KEY`
- Default models: `glm-5.1`, `glm-5-turbo`
- `supports_reasoning_effort`: false; thinking enabled by default via `thinking: {type: "enabled", clear_thinking: false}`

### `openrouter` — `openai_chat.py`

- SDK: `openai` against OpenRouter's OpenAI-compatible endpoint
- Base URL: `https://openrouter.ai/api/v1`
- API key env: `OPENROUTER_API_KEY`
- Default models: `openai/gpt-5.2`, `anthropic/claude-sonnet-4.6`
- `supports_reasoning_effort`: true (forwarded through `extra_body.reasoning.effort`)

## Reasoning Effort Mapping

| effort   | anthropic / moonshotai / minimax | openai   | openrouter |
| -------- | -------------------------------- | -------- | ---------- |
| `none`   | thinking disabled                | `none`   | `none`     |
| `low`    | low budget_tokens                | `low`    | `low`      |
| `medium` | medium budget_tokens             | `medium` | `medium`   |
| `high`   | high budget_tokens               | `high`   | `high`     |
| `xhigh`  | `high` (sonnet) / `max` (opus)   | `xhigh`  | `xhigh`    |

`reasoning_effort` is only applied when both `adapter.supports_reasoning_effort` and `model_metadata.supports_reasoning` (from models.dev) are true.
