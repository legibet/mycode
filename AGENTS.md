# AGENTS.md - mycode

This file is the authoritative project context for future agent runs. Keep it in sync with the actual code.

## 1. Product

`mycode` is a personal minimal coding agent with a web UI and CLI.

Current priorities:

- small, readable core
- one internal conversation model
- one agent loop
- append-only sessions
- provider adapters at the boundary only

The project is intentionally not a general agent framework.

## 2. Core Rules

- Only 4 built-in tools exist: `read`, `write`, `edit`, `bash`
- Do not add `grep`, `glob`, or extra search tools to core
- Keep the runtime deterministic and easy to inspect
- Prefer simple Python over abstractions that hide control flow
- Keep provider-specific behavior inside adapters, not inside the agent loop

## 3. Runtime Shape

The current runtime no longer uses `any-llm`.

It is built from:

- one internal message/block format in `mycode/core/messages.py`
- one agent loop in `mycode/core/agent.py`
- provider adapters in `mycode/core/providers/`

Both CLI and server import the same core runtime.

## 4. Internal Message Model

The persisted/runtime message format is block-based JSON:

```json
{
  "role": "assistant",
  "content": [
    {"type": "thinking", "text": "...", "meta": {}},
    {"type": "text", "text": "..."},
    {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}
  ],
  "meta": {
    "provider": "moonshotai",
    "model": "kimi-k2.5"
  }
}
```

Current block types in active use:

- `text`
- `thinking`
- `tool_use`
- `tool_result`

Important notes:

- thinking is first-class session data and is persisted
- assistant message metadata is normalized as `provider` / `model` / `provider_message_id` / `stop_reason` / `usage`
- provider-native assistant-message extras live under `meta.native`
- provider-native block replay hints live under `block.meta.native`
- user tool results are stored as a `user` message containing `tool_result` blocks
- the system prompt is runtime-only and is not persisted into sessions

## 5. Agent Loop

`mycode/core/agent.py` is the only orchestration loop.

Per turn it does this:

1. append the user message
2. ask the selected provider adapter for one assistant turn
3. stream normalized events to CLI/server
4. persist the final assistant message
5. execute any requested tools locally
6. append one `user` tool-result message
7. continue until the assistant stops using tools; `max_turns` is optional and defaults to no loop cap

Other current behaviors:

- interrupted prior tool calls are repaired with synthetic `tool_result` error blocks when sessions are loaded
- cancelling stops the in-flight provider stream and actively kills running `bash` subprocesses via `cancel_all_tools()`
- tool output is streamed only for `bash`

## 6. Provider Adapters

Provider lookup lives in `mycode/core/providers/lookup.py`.

Current built-in adapter ids:

- `anthropic`
- `deepseek`
- `moonshotai`
- `minimax`
- `openai`
- `openai_chat`
- `openrouter`
- `zai`

### `anthropic`

- implemented with the official `anthropic` Python SDK
- uses the Messages API
- default base URL: `https://api.anthropic.com`
- `claude-sonnet-4-6` and `claude-opus-4-6` use Anthropic's adaptive thinking flow when `reasoning_effort` is set
- other Claude reasoning models use manual extended thinking with `thinking = {"type": "enabled", "budget_tokens": ...}`
- `reasoning_effort = xhigh` maps to `high` for `claude-sonnet-4-6` and to `max` for `claude-opus-4-6`; older Claude reasoning models keep the manual extended-thinking budget mapping
- Anthropic-style message adapters now add ephemeral `cache_control` to the system prompt block and the last user content block

### `moonshotai`

- implemented with the official `anthropic` Python SDK against Moonshot's Anthropic-compatible endpoint
- default base URL: `https://api.moonshot.ai/anthropic`
- default API key env: `MOONSHOT_API_KEY`
- when `reasoning_effort` is set, the adapter maps it to Anthropic-style manual `budget_tokens`
- prior reasoning must be replayed on later tool-loop turns when thinking is enabled
- shares the Anthropic-like ephemeral prompt cache markers used by the direct Anthropic adapter

### `minimax`

- implemented with the official `anthropic` Python SDK against MiniMax's Anthropic-compatible endpoint
- default base URL: `https://api.minimax.io/anthropic`
- default API key env: `MINIMAX_API_KEY`
- preserves provider-native thinking signatures in block metadata
- when `reasoning_effort` is set, the adapter maps it to Anthropic-style manual `budget_tokens`
- shares the Anthropic-like ephemeral prompt cache markers used by the direct Anthropic adapter

### `openai`

- implemented with the official `openai` Python SDK
- uses the Responses API
- default base URL: `https://api.openai.com/v1`
- GPT-5 family reasoning uses OpenAI's official `reasoning = {"effort": ...}` parameter with supported values `none/low/medium/high/xhigh`
- tool loops continue with `previous_response_id` + `function_call_output`
- this adapter expects prior assistant messages from the same provider/session so it can reuse `provider_message_id`
- requests also pass `prompt_cache_key` using the current session id

### `openai_chat`

- implemented with the official `openai` Python SDK
- uses Chat Completions
- intended for third-party OpenAI-compatible providers when Responses API is unavailable
- does not apply the shared `reasoning_effort` setting; unsupported third-party chat providers keep their upstream default behavior
- preserves common third-party reasoning extensions such as `reasoning_content` and `reasoning_details` when exposed through SDK extras
- current real-provider validation used Moonshot and MiniMax OpenAI-compatible chat endpoints

### `deepseek`

- implemented with the official `openai` Python SDK against DeepSeek's OpenAI-compatible chat endpoint
- default base URL: `https://api.deepseek.com`
- default API key env: `DEEPSEEK_API_KEY`
- default models: `deepseek-chat`, `deepseek-reasoner`
- does not apply the shared `reasoning_effort` setting; requests keep DeepSeek's default thinking behavior
- `reasoning_content` is replayed only on tool-loop continuation turns, matching DeepSeek's documented thinking/tool-calling flow

### `zai`

- implemented with the official `openai` Python SDK against Z.AI's international OpenAI-compatible chat endpoint
- default base URL: `https://api.z.ai/api/paas/v4/`
- default API key env: `ZAI_API_KEY`
- default models: `glm-5`, `glm-4.7`
- does not apply the shared `reasoning_effort` setting; requests keep Z.AI's default thinking behavior
- `reasoning_content` is replayed only on tool-loop continuation turns for compatibility with GLM thinking/tool-calling flows

### `openrouter`

- implemented with the official `openai` Python SDK against OpenRouter's OpenAI-compatible chat endpoint
- default base URL: `https://openrouter.ai/api/v1`
- default API key env: `OPENROUTER_API_KEY`
- default models: `openai/gpt-5.2`, `anthropic/claude-sonnet-4.6`
- `reasoning_effort` is forwarded through `extra_body.reasoning.effort`, letting OpenRouter normalize it for the upstream model
- although OpenRouter ships its own Python SDK, the runtime intentionally uses the OpenAI-compatible chat API to keep the agent loop thin and provider behavior explicit

## 7. Tool Schema

`mycode/core/tools.py` is still the source of truth for built-in tool definitions.

Current internal schema shape is:

- `name`
- `description`
- `input_schema`

Adapters convert this to the upstream provider format:

- Anthropic-style `input_schema`
- OpenAI Chat `function.parameters`
- OpenAI Responses `function.parameters`

`tools.py` also owns:

- output truncation rules
- path resolution
- exact-match edit behavior with conservative fuzzy fallback
- large bash output spilling into `tool-output/`
- `parse_tool_arguments()` used by OpenAI-family adapters

## 8. Sessions

`mycode/core/session.py` stores sessions under:

```text
~/.mycode/sessions/<session_id>/
  meta.json
  messages.jsonl
  tool-output/
```

Current facts:

- append-only JSONL
- current `MESSAGE_FORMAT_VERSION = 4`
- session meta stores `provider`, `model`, `cwd`, `api_base`, and `message_format_version`
- the first user message auto-updates the title from text content
- `get_or_create()` preserves existing session meta; request-time provider/model overrides are runtime-only

The runtime expects the current internal block-based message format.

## 9. Config

`mycode/core/config.py` loads config from:

- `~/.mycode/config.json`
- `<workspace>/.mycode/config.json`

Important behavior:

- explicit request args override config
- API keys may come from provider-specific env vars
- `providers.<name>.api_key` may also reference an exact env var with `${ENV_NAME}`
- when config uses `${ENV_NAME}`, that referenced env var takes priority over the provider's built-in default API key env var
- provider/model/base URL are not loaded from env vars automatically
- raw provider ids are allowed if they exist in the registry
- fallback provider/model are currently `anthropic` + `claude-sonnet-4-6`

`ProviderConfig.type` is the internal adapter id, not a generic vendor label.

### Reasoning effort

`reasoning_effort` controls how much thinking a model does. The unified options are:

- `auto` — do not pass any effort parameter; let the provider decide (default when unconfigured)
- `none` — explicitly disable thinking
- `low` / `medium` / `high` / `xhigh` — explicit effort levels

Config-file resolution order: `providers.<name>.reasoning_effort` > `default.reasoning_effort`. The resolved value is only applied when both `adapter.supports_reasoning_effort` and `model_metadata.supports_reasoning` (from models.dev) are true.

CLI and web frontend can override reasoning effort at runtime without changing config files. These overrides are per-request and are not persisted into session metadata.

### Model metadata

`mycode/core/models.py` fetches and caches the models.dev catalog (`api.json`) to look up per-model capabilities such as `supports_reasoning`, `context_window`, and `max_output_tokens`. The cache lives at `~/.mycode/cache/models.dev-api.json` with a 24-hour TTL. Requests to models.dev require a `User-Agent` header to avoid 403 responses.

## 10. Interfaces

### Server

`mycode/server/routers/chat.py`

- streams SSE from the shared `Agent`
- persists each message through `SessionStore`
- keeps SSE event names stable
- `POST /chat` accepts optional `reasoning_effort`; when provided it overrides the config-resolved value
- `GET /config` returns per-provider reasoning metadata: `supports_reasoning_effort`, `reasoning_models` (subset from models.dev), `reasoning_effort` (config value), plus top-level `default_reasoning_effort` and `reasoning_effort_options`

`mycode/server/routers/sessions.py`

- creates/list/loads/deletes sessions using the shared store

### CLI

`mycode/cli/main.py`

- defaults to interactive mode with `mycode`
- supports one-shot runs with `mycode run "..."`
- supports web serving with `mycode web`
- `mycode web --dev` starts the backend in API-only mode for Vite frontend development
- contains the terminal entry flow, rendering, and interactive chat loop
- default startup creates a fresh session
- resume is explicit via `--continue`, `--session`, or `/resume`
- shows thinking during live runs
- history preview also includes persisted thinking summaries when assistant text is absent
- interactive slash commands: `/clear`, `/new`, `/resume`, `/provider`, `/model`, `/effort`, `/q`
- `/effort` allows runtime reasoning effort selection; shows "current model does not support reasoning effort" when the provider+model combination does not support it
- the session header displays the active reasoning effort when set

Development and release workflow:

- `uv sync --dev` is the default Python development setup
- `pnpm --dir frontend dev` runs the Vite frontend during web UI development
- `uv run mycode web --dev` is the backend companion for Vite dev mode
- `uv run --no-project python scripts/build_frontend.py` refreshes packaged frontend assets in-repo
- `uv build` builds the frontend and packages `mycode/server/static/` into wheel/sdist artifacts

### Frontend

Current frontend message reconstruction is in `frontend/src/utils/messages.js`.

Frontend source lives in the top-level `frontend/` app. Built assets are copied into `mycode/server/static/` for packaged web serving.

Serving modes:

- `mycode web` serves the packaged frontend from `mycode/server/static/`
- `mycode web --dev` does not mount packaged frontend assets and only serves the API backend

Current behavior:

- `useChat` stores raw block-based conversation messages plus ephemeral tool runtime state
- `buildRenderMessages()` derives UI messages from canonical blocks instead of maintaining a second source of truth
- assistant `thinking` blocks render as reasoning blocks
- assistant `tool_use` blocks render directly, with persisted `tool_result` blocks and live tool runtime folded in at render time
- reasoning blocks default to expanded UI state in `frontend/src/components/Chat/ReasoningBlock.jsx`
- sidebar settings panel includes provider, model, and conditional reasoning effort selector
- the effort selector only renders when the current provider+model supports reasoning effort (determined by `supports_reasoning_effort` and `reasoning_models` from the config endpoint)
- frontend config (provider, model, cwd, reasoningEffort) is persisted to localStorage; `auto` and empty both mean "do not send effort to server"

## 11. SSE Contract

The outer event contract used by server, CLI, and frontend remains:

- `reasoning`
- `text`
- `tool_start`
- `tool_output`
- `tool_done`
- `error`

Current payload fields:

- `reasoning`: `delta`
- `text`: `delta`
- `tool_start`: `tool_call` with `id` / `name` / `input`
- `tool_output`: `tool_use_id` + `output`
- `tool_done`: `tool_use_id` + `result` + `is_error`
- `error`: `message`

Do not change these casually.

## 12. Dependencies

Runtime Python deps currently include:

- `anthropic`
- `openai`
- `fastapi`
- `uvicorn`
- `rich`
- `prompt-toolkit`

Package management and execution conventions:

- use `uv` for Python
- use `pnpm` for frontend

## 13. Verified Provider Facts

These have been validated during this redesign:

- Moonshot recommends Anthropic-compatible Messages for coding-agent style development
- MiniMax officially documents Anthropic SDK / Messages compatibility and explicitly says full assistant content must be appended on multi-turn function-call flows
- Moonshot `kimi-k2.5` tool loops work through the Anthropic-compatible endpoint, and prior reasoning must be preserved when thinking is enabled
- MiniMax `MiniMax-M2.5` emits thinking signatures on the Anthropic-compatible endpoint
- third-party OpenAI-compatible chat endpoints may surface reasoning through non-standard extra fields rather than a uniform schema
- DeepSeek `deepseek-reasoner` thinks by default without any explicit parameter; `deepseek-chat` does not think unless `thinking: {"type": "enabled"}` is sent
- Z.AI GLM models (glm-5, glm-4.7) think by default; explicit parameter is only needed to disable thinking
- third-party thinking control parameters are not standardized: DeepSeek/Z.AI use `thinking: {type}`, Qwen uses `enable_thinking: bool`, others vary

## 14. Guardrails

When changing architecture, preserve these unless explicitly asked otherwise:

- 4-tool core stays unchanged
- append-only sessions stay human-inspectable
- CLI and server remain thin wrappers over `mycode.core`
- provider-specific quirks stay in adapters
- no new framework-style abstraction layers unless they remove real complexity

When in doubt, prefer the simpler and more explicit design.
