# mycode — Project Context

Authoritative context for agent runs. Keep in sync with the code. See `docs/` for detailed specs.

## Product

`mycode` is a personal minimal coding agent with a web UI and CLI.

Priorities: small readable core · one message model · one agent loop · append-only sessions · provider adapters at the boundary.

Not a general agent framework.

## Core Rules

- 4 built-in tools only: `read`, `write`, `edit`, `bash` — do not add more to core
- Provider-specific behavior stays inside adapters, never in the agent loop
- Prefer simple Python; add helpers only for real reuse or non-obvious logic
- Keep the runtime deterministic and easy to inspect

## Source Map

Core runtime (`mycode/core/`):

- `agent.py` — the only orchestration loop
- `messages.py` — internal message/block format
- `tools.py` — 4 built-in tools, executor, truncation, path resolution
- `session.py` — append-only JSONL session storage, compact/rewind events, interrupted tool repair
- `config.py` — layered config loading and provider resolution
- `models.py` — bundled model metadata lookup (`context_window`, `supports_reasoning`, `supports_image_input`)
- `system_prompt.py` — runtime system prompt assembly, AGENTS.md discovery, skills discovery
- `system_prompt.md` — system prompt template
- `providers/base.py` — ProviderAdapter abstract interface
- `providers/__init__.py` — adapter registry and provider lookup helpers
- `providers/anthropic_like.py` — adapters: `anthropic`, `moonshotai`, `minimax`
- `providers/gemini.py` — adapter: `google`
- `providers/openai_responses.py` — adapter: `openai`
- `providers/openai_chat.py` — adapters: `openai_chat`, `deepseek`, `zai`, `openrouter`

CLI (`mycode/cli/`):

- `main.py` — Typer entrypoint (commands: default, run, web, session)
- `chat.py` — TerminalChat interactive loop
- `render.py` — TerminalView rich rendering
- `runtime.py` — build_agent(), resolve_session()

Server (`mycode/server/`):

- `app.py` — FastAPI factory, static mount
- `routers/chat.py` — POST /api/chat, GET /api/runs/{id}/stream, POST /api/runs/{id}/cancel, GET /api/config
- `routers/sessions.py` — session CRUD
- `routers/workspaces.py` — directory browser
- `run_manager.py` — concurrent run management
- `schemas.py` — Pydantic request/response models

Web UI (`web/src/`):

- `hooks/useChat.ts` — chat state, SSE streaming, tool runtime
- `utils/messages.ts` — buildRenderMessages() — canonical blocks → UI messages

## Internal Message Model

Block-based JSON — single format used at runtime and persisted to sessions:

```json
{
  "role": "assistant",
  "content": [
    {"type": "thinking", "text": "...", "meta": {}},
    {"type": "text", "text": "..."},
    {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.py"}}
  ],
  "meta": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "stop_reason": "tool_use",
    "usage": {},
    "native": {}
  }
}
```

Block types: `text` · `image` · `thinking` · `tool_use` · `tool_result`

- `thinking` blocks are first-class session data — persisted and shown in UI
- Provider-specific extras: `meta.native` on messages, `block.meta.native` on blocks
- Tool results stored as a `user` message with `tool_result` blocks:

  ```json
  {"type": "tool_result", "tool_use_id": "call_1", "model_text": "ok", "display_text": "Wrote x.py", "is_error": false}
  ```

  `model_text` is replayed to providers on later turns; `display_text` is shown to users.
  `tool_result.content` may store structured `text` and `image` blocks.
- System prompt is runtime-only, not persisted

## Agent Loop

`mycode/core/agent.py` — per user turn:

1. Append user message to session
2. Call provider adapter → stream events to CLI/server
3. Persist assistant message to JSONL
4. Execute tool calls locally
5. Append `user` tool-result message
6. Repeat until no tool calls; `max_turns` defaults to unlimited
7. Optionally compact context when usage ≥ `compact_threshold` (default 0.8)

## Provider Adapters

See `docs/providers.md` for per-adapter details, env vars, and quirks.

| id            | protocol                      | file                  |
| ------------- | ----------------------------- | --------------------- |
| `anthropic`   | Anthropic Messages API        | `anthropic_like.py`   |
| `moonshotai`  | Anthropic-compatible endpoint | `anthropic_like.py`   |
| `minimax`     | Anthropic-compatible endpoint | `anthropic_like.py`   |
| `google`      | Google genai SDK              | `gemini.py`           |
| `openai`      | OpenAI Responses API          | `openai_responses.py` |
| `openai_chat` | OpenAI Chat Completions       | `openai_chat.py`      |
| `deepseek`    | OpenAI-compatible chat        | `openai_chat.py`      |
| `zai`         | OpenAI-compatible chat        | `openai_chat.py`      |
| `openrouter`  | OpenAI-compatible chat        | `openai_chat.py`      |

All adapters implement `ProviderAdapter.stream_turn()`. Message projection to provider wire format lives in `prepare_messages()`.

## SSE Contract

**Do not change event names or payload shapes without updating server, CLI, and web UI.**

| event         | payload                                                 |
| ------------- | ------------------------------------------------------- |
| `reasoning`   | `delta`                                                 |
| `text`        | `delta`                                                 |
| `tool_start`  | `tool_call: {id, name, input}`                          |
| `tool_output` | `tool_use_id`, `output`                                 |
| `tool_done`   | `tool_use_id`, `model_text`, `display_text`, `is_error` |
| `compact`     | `message`                                               |
| `error`       | `message`                                               |

## Detailed Docs

- `docs/api.md` — Server API endpoints, request/response schemas, SSE contract details
- `docs/config.md` — Config files, schema, API key resolution, reasoning effort, skills/instructions discovery
- `docs/providers.md` — Per-adapter details: SDK, base URL, env vars, reasoning effort mapping, quirks
- `docs/sessions.md` — Storage layout, JSONL record types, compact/rewind/repair, format version
- `docs/web.md` — Component structure, message state model, build process

## Interfaces

**CLI** — `mycode/cli/main.py`:

- `mycode` — interactive session (default)
- `mycode run "..."` — non-interactive single run
- `mycode web [--dev]` — web server; `--dev` serves API only (for Vite dev)
- `mycode session list` — list sessions
- Interactive CLI: `@path` attaches files; images become `image` blocks, text files become extra `text` blocks
- Slash commands: `/clear` `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/q`

**Server** — `mycode/server/routers/`:

- `POST /api/chat` — start a run from `message` or `input`; returns `{run, session}` JSON immediately
- `GET /api/runs/{run_id}/stream` — SSE stream for a run
- `POST /api/runs/{run_id}/cancel` — cancel a run
- `GET /api/config` — provider, reasoning, and image-input metadata for the web UI
- Session CRUD at `/api/sessions`
- Workspace browser at `/api/workspaces`

## Dev Workflow

```bash
uv sync --dev                                          # Python setup
uv run mycode                                          # run CLI
uv run mycode web --dev                                # API only (backend for Vite dev)
pnpm --dir web test:run                                # run web UI tests once
pnpm --dir web dev                                     # Vite web UI dev server
uv run --no-project python scripts/build_web.py       # rebuild packaged web UI
uv build                                               # build wheel + sdist
```

## Guardrails

Preserve unless explicitly asked to change:

- 4-tool core stays unchanged
- Append-only sessions stay human-inspectable
- CLI and server remain thin wrappers over `mycode.core`
- Provider-specific quirks stay in adapters
- No new abstraction layers unless they remove real complexity

When in doubt, prefer the simpler and more explicit design.
