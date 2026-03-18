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
    "provider": "moonshot",
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
- provider-native metadata is stored in `meta`
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
7. continue until the assistant stops using tools or `max_turns` is reached

Other current behaviors:

- interrupted prior tool calls are repaired with synthetic `tool_result` error blocks on startup
- cancelling during `bash` actively kills subprocesses via `cancel_all_tools()`
- tool output is streamed only for `bash`

## 6. Provider Adapters

Provider lookup lives in `mycode/core/provider_registry.py`.

Current built-in adapter ids:

- `anthropic`
- `moonshot`
- `minimax`
- `openai`
- `openai_chat`

### `anthropic`

- implemented with the official `anthropic` Python SDK
- uses the Messages API
- default base URL: `https://api.anthropic.com`

### `moonshot`

- implemented with the official `anthropic` Python SDK against Moonshot's Anthropic-compatible endpoint
- default base URL: `https://api.moonshot.ai/anthropic`
- default API key env: `MOONSHOT_API_KEY`
- for `kimi-k2.5`, the adapter explicitly enables thinking by default
- real-provider testing showed that when thinking is enabled, prior reasoning must be replayed on later tool-loop turns

### `minimax`

- implemented with the official `anthropic` Python SDK against MiniMax's Anthropic-compatible endpoint
- default base URL: `https://api.minimax.io/anthropic`
- default API key env: `MINIMAX_API_KEY`
- preserves provider-native thinking signatures in block metadata
- by default it relies on MiniMax's native thinking behavior unless an explicit reasoning mode is requested

### `openai`

- implemented with the official `openai` Python SDK
- uses the Responses API
- default base URL: `https://api.openai.com/v1`
- tool loops continue with `previous_response_id` + `function_call_output`
- this adapter expects prior assistant messages from the same provider/session so it can reuse `provider_message_id`

### `openai_chat`

- implemented with the official `openai` Python SDK
- uses Chat Completions
- intended for third-party OpenAI-compatible providers when Responses API is unavailable
- preserves common third-party reasoning extensions such as `reasoning_content` and `reasoning_details` when exposed through SDK extras
- current real-provider validation used Moonshot and MiniMax OpenAI-compatible chat endpoints

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
mycode/data/sessions/<session_id>/
  meta.json
  messages.jsonl
  tool-output/
```

Current facts:

- append-only JSONL
- current `MESSAGE_FORMAT_VERSION = 2`
- session meta stores `provider`, `model`, `cwd`, `api_base`, and `message_format_version`
- the first user message auto-updates the title from text content
- `get_or_create()` keeps meta in sync with the latest request config

The runtime expects the current internal block-based message format.

## 9. Config

`mycode/core/config.py` loads config from:

- `~/.mycode/config.json`
- `<workspace>/.mycode/config.json`

Important behavior:

- explicit request args override config
- API keys may come from provider-specific env vars
- provider/model/base URL are not loaded from env vars automatically
- raw provider ids are allowed if they exist in the registry
- fallback provider/model are currently `anthropic` + `claude-sonnet-4-6`

`ProviderConfig.type` is the internal adapter id, not a generic vendor label.

## 10. Interfaces

### Server

`mycode/server/routers/chat.py`

- streams SSE from the shared `Agent`
- persists each message through `SessionStore`
- keeps SSE event names stable

`mycode/server/routers/sessions.py`

- creates/list/loads/deletes sessions using the shared store

### CLI

`mycode/cli.py`

- supports interactive mode and `--once`
- default startup creates a fresh session
- resume is explicit via `--continue`, `--session`, or `/resume`
- shows thinking during live runs
- history preview also includes persisted thinking summaries when assistant text is absent

### Frontend

Current frontend message reconstruction is in `mycode/frontend/src/utils/messages.js`.

Current behavior:

- assistant `thinking` blocks render as reasoning parts
- assistant `tool_use` blocks become tool parts
- later `user` `tool_result` blocks are attached back onto the matching assistant tool part
- reasoning blocks default to expanded UI state in `mycode/frontend/src/components/Chat/ReasoningBlock.jsx`

## 11. SSE Contract

The outer event contract used by server, CLI, and frontend remains:

- `reasoning`
- `text`
- `tool_start`
- `tool_output`
- `tool_done`
- `error`

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

## 14. Guardrails

When changing architecture, preserve these unless explicitly asked otherwise:

- 4-tool core stays unchanged
- append-only sessions stay human-inspectable
- CLI and server remain thin wrappers over `mycode.core`
- provider-specific quirks stay in adapters
- no new framework-style abstraction layers unless they remove real complexity

When in doubt, prefer the simpler and more explicit design.
