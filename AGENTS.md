# mycode-go — Project Context

Authoritative context for agent runs. Keep this file in sync with the Go code.

## Product

`mycode-go` is a personal minimal coding agent with a web UI and a small CLI. It keeps `.mycode` config and session compatibility with the original Python version.

Priorities:

- small readable core
- one message model
- one agent loop
- append-only sessions
- provider adapters at the boundary

It is not a general agent framework.

## Core Rules

- 4 built-in tools only: `read`, `write`, `edit`, `bash`
- Provider-specific behavior stays inside adapters
- Keep the runtime explicit and easy to inspect
- Prefer simple Go over framework-heavy designs
- Do not add abstraction layers unless they remove real complexity

## Source Map

Core runtime:

- `mycode-go/internal/agent/agent.go` — the only orchestration loop
- `mycode-go/internal/message/message.go` — canonical message/block format
- `mycode-go/internal/tools/*.go` — 4 built-in tools and execution
- `mycode-go/internal/session/store.go` — append-only JSONL storage, compact, rewind, repair
- `mycode-go/internal/config/*.go` — layered config loading and provider resolution
- `mycode-go/internal/models/catalog.go` — bundled model metadata lookup
- `mycode-go/internal/prompt/prompt.go` — runtime system prompt assembly, AGENTS discovery, skills discovery

Providers:

- `mycode-go/internal/provider/base.go` — adapter contract and replay helpers
- `mycode-go/internal/provider/registry.go` — adapter registry
- `mycode-go/internal/provider/specs.go` — built-in provider metadata
- `mycode-go/internal/provider/anthropic.go` — `anthropic`, `moonshotai`, `minimax`
- `mycode-go/internal/provider/openai_responses.go` — `openai`
- `mycode-go/internal/provider/openai_chat.go` — `openai_chat`, `deepseek`, `zai`, `openrouter`
- `mycode-go/internal/provider/google.go` — `google`

Server:

- `mycode-go/internal/server/app.go` + `mycode-go/internal/server/*.go` — HTTP API, SSE, static web serving, request parsing
- `mycode-go/internal/server/run_manager.go` — concurrent run manager
- `mycode-go/internal/server/types.go` — request/response payload types
- `mycode-go/internal/workspace/workspace.go` — workspace browser

CLI:

- `mycode-go/cmd/mycode-go/*.go` — `run`, `web`, `session list`, and bare-message convenience mode

Web UI:

- `web/src/hooks/useChat.ts` — chat state and SSE streaming
- `web/src/utils/messages.ts` — canonical blocks to UI messages

## Internal Message Model

All runtime, persistence, and API data use the same block-based JSON format:

```json
{
  "role": "assistant",
  "content": [
    {"type": "thinking", "text": "..."},
    {"type": "text", "text": "..."},
    {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "x.go"}}
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

Block types:

- `text`
- `image`
- `document`
- `thinking`
- `tool_use`
- `tool_result`

Tool results are stored as a `user` message with `tool_result` blocks. `thinking` blocks are first-class session data.

## Agent Loop

`mycode-go/internal/agent/agent.go` runs one user turn:

1. Append user message
2. Stream one provider turn
3. Persist the assistant message
4. Execute tool calls locally
5. Append tool results as a `user` message
6. Repeat until there are no tool calls
7. Optionally compact context when usage crosses `compact_threshold`

## Provider Types

See `docs/providers.md` for details. All provider ids are preserved:

- `anthropic`
- `moonshotai`
- `minimax`
- `openai`
- `openai_chat`
- `deepseek`
- `zai`
- `openrouter`
- `google`

## SSE Contract

Do not change these event names or shapes without updating server and web UI.

- `reasoning` — `delta`
- `text` — `delta`
- `tool_start` — `tool_call: {id, name, input}`
- `tool_output` — `tool_use_id`, `output`
- `tool_done` — `tool_use_id`, `model_text`, `display_text`, `is_error`
- `compact` — `message`
- `error` — `message`

Every event also carries `seq`.

## Interfaces

CLI:

- `mycode-go <message>` — convenience alias for one non-interactive run
- `mycode-go run "..."` — one non-interactive run
- `mycode-go web [--dev]` — web server
- `mycode-go session list` — list sessions

This Go rewrite does not include the old terminal TUI.

Server:

- `POST /api/chat`
- `GET /api/runs/{run_id}/stream`
- `POST /api/runs/{run_id}/cancel`
- `GET /api/config`
- session CRUD at `/api/sessions`
- workspace browser at `/api/workspaces`

## Commit Conventions

`web/` changes and backend changes must be in **separate commits**.

Commit message format: `type(scope): description`

Scopes:

- `web` — changes under `web/` only
- `backend` — Go backend changes only
- `cli` — CLI changes only
- no scope — cross-cutting (document both sides in commit body)

When syncing web changes from `main`:

```bash
# find web-only commits on main since last sync
git log main --oneline -- web/

# cherry-pick a specific web commit
git cherry-pick <hash>
```

## Dev Workflow

Backend:

```bash
go -C mycode-go test ./...
go -C mycode-go vet ./...
cd mycode-go && golangci-lint run ./...
go -C mycode-go run ./cmd/mycode-go web --dev
uv run --no-project python ./scripts/update_models_catalog.py
```

Web:

```bash
pnpm --dir web install
pnpm --dir web typecheck
pnpm --dir web test:run
pnpm --dir web dev
pnpm --dir web build
```

## Guardrails

Preserve unless explicitly asked to change:

- 4-tool core stays unchanged
- append-only sessions stay human-inspectable
- CLI and server stay thin wrappers over `mycode-go/internal/agent`
- provider quirks stay in adapters
- no unnecessary abstraction layers
