# mycode — Project Context

Authoritative context for agent runs on this project. Keep in sync with the code. See `docs/` for detailed specs.

## Product

`mycode` is a personal minimal coding agent shipped as two PyPI packages:

- `mycode-sdk` (import `mycode`) — the runtime: agent loop, message format, session store, provider adapters, and built-in tools. Lightweight, suitable for embedding the agent in other Python apps.
- `mycode-cli` (import `mycode_cli`) — the interactive CLI and FastAPI web server built on top of the SDK.

Priorities: small readable core · one message model · one agent loop · append-only sessions · provider adapters at the boundary.

## Core Rules

- 4 built-in tools only: `read`, `write`, `edit`, `bash` — do not add more to the SDK
- Provider-specific behavior stays inside adapters, never in the agent loop
- Prefer simple Python; add helpers only for real reuse or non-obvious logic
- Keep the runtime deterministic and easy to inspect

## Source Map

SDK package — `mycode/src/mycode/`:

- `__init__.py` — public API re-exports (`Agent`, `tool`, built-in tool constants, message helpers, …)
- `agent.py` — agent loop (`Agent`)
- `messages.py` — internal message/block format
- `tools.py` — `ToolSpec`, `ToolExecutor`, `ToolContext`, the four built-in tools, `@tool` decorator
- `session.py` — append-only JSONL session storage, compact/rewind events, interrupted tool repair, `resolve_mycode_home`/`resolve_sessions_dir`
- `models.py` + `models_catalog.json` — bundled model metadata lookup
- `utils.py` — small typed helpers (`as_int`, `as_bool`, `omit_none`, `parse_tool_arguments`)
- `providers/base.py` — `ProviderAdapter` abstract interface
- `providers/__init__.py` — adapter registry and provider lookup helpers
- `providers/anthropic_like.py` — adapters: `anthropic`, `moonshotai`, `minimax`
- `providers/gemini.py` — adapter: `google`
- `providers/openai_responses.py` — adapter: `openai`
- `providers/openai_chat.py` — adapters: `openai_chat`, `deepseek`, `zai`, `openrouter`

CLI package — `cli/src/mycode_cli/`:

- `main.py` — Typer entrypoint (commands: default, run, web, session)
- `tui/chat.py` — TerminalChat interactive loop
- `tui/render.py` — TerminalView rich rendering
- `tui/theme.py` — terminal theme detection and color tokens
- `runtime.py` — `build_agent()`, `resolve_session()`
- `config.py` — layered TOML/JSON config loading and provider resolution (CLI/server only)
- `system_prompt.py` — runtime system prompt assembly: inlined base prompt + AGENTS.md + skills discovery
- `server/app.py` — FastAPI factory, static mount
- `server/routers/chat.py` — POST /api/chat, GET /api/runs/{id}/stream, POST /api/runs/{id}/cancel, GET /api/config
- `server/routers/sessions.py` — session CRUD
- `server/routers/workspaces.py` — directory browser
- `server/run_manager.py` — concurrent run management
- `server/schemas.py` — Pydantic request/response models

Web UI — `web/src/`:

- `hooks/useChat.ts` — chat state, SSE streaming, tool runtime
- `utils/messages.ts` — `buildRenderMessages()` — canonical blocks → UI messages

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

`mycode/src/mycode/agent.py` — per user turn:

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
- `docs/sdk.md` — Public SDK surface, `Agent`, `@tool`, `SessionStore`
- `docs/sessions.md` — Storage layout, JSONL record types, compact/rewind/repair, format version
- `docs/web.md` — Component structure, message state model, build process

## Interfaces

**CLI** — `cli/src/mycode_cli/main.py`:

- `mycode` — interactive session (default)
- `mycode run "..."` — non-interactive single run
- `mycode web [--dev]` — web server; `--dev` serves API only (for Vite dev)
- `mycode session list` — list sessions
- Interactive CLI: `@path` attaches files; images become `image` blocks, text files become extra `text` blocks
- Slash commands: `/clear` `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/q`

**Server** — `cli/src/mycode_cli/server/routers/`:

- `POST /api/chat` — start a run from `message` or `input`; returns `{run, session}` JSON immediately
- `GET /api/runs/{run_id}/stream` — SSE stream for a run
- `POST /api/runs/{run_id}/cancel` — cancel a run
- `GET /api/config` — provider, reasoning, and image-input metadata for the web UI
- Session CRUD at `/api/sessions`
- Workspace browser at `/api/workspaces`

## Commit Conventions

Commit message format: `type(scope): description`

Scopes:

- `web` — changes under `web/` only
- `sdk` — SDK package (`mycode/`) only
- `cli` — CLI/server package (`cli/`) only
- no scope — cross-cutting changes (e.g. SSE contract changes that touch both sides — document what changed in both in the commit body)

Examples:

```
feat(web): add tool duration display
fix(sdk): handle empty tool result in compact
feat(sdk): add tool decorator
refactor(cli): unify provider switcher
docs: update SSE contract in AGENTS.md
```

When a feature requires both web and CLI changes, make two commits.

## Dev Workflow

```bash
uv sync --dev                                          # install/update Python deps
pnpm --dir web install                                # install web deps

uv run mycode                                          # start the CLI
uv run mycode web --dev                                # backend API for Vite dev
pnpm --dir web dev                                     # frontend Vite dev server

uv run basedpyright                                    # Python type checking
pnpm --dir web typecheck                               # web type checking
uv run pytest                                          # Python tests
pnpm --dir web test:run                                # web tests

uv build --package mycode-sdk                          # build SDK package
uv build --package mycode-cli                          # build CLI package
```

Useful shortcuts:

```bash
just dev                                               # start backend API and Vite dev server together
just check                                             # run ruff check, basedpyright, web typecheck, and biome check
just test                                              # run Python tests and web tests
just fmt                                               # run ruff fix/format and biome check --write
```

## Guardrails

Preserve unless explicitly asked to change:

- 4 built-in tools stay unchanged
- Append-only sessions stay human-inspectable
- CLI and server remain thin wrappers over the `mycode` SDK
- Provider-specific quirks stay in adapters
- No new abstraction layers unless they remove real complexity

When in doubt, prefer the simpler and more explicit design.
