# mycode-go — Agent Context

Always-loaded context for agent runs on this branch. Detailed specs live in `docs/`; this file points at them rather than duplicating their content.

## Product

`mycode-go` is the Go rewrite of `mycode`, implemented as reusable Go packages plus app adapters:

- Public packages — `agent`, `message`, `attachment`, `tools`, `session`, and `provider`: the runtime, canonical message format, attachment conversion, tool execution, append-only sessions, and provider adapters.
- `cmd/mycode-go` + `internal/*` — the CLI, HTTP server, config loading, system prompt, permission policy, run manager, and web adapter built on top of the public packages.
- `web/` — the shared React + Vite UI.

Current sync: Python `main` provider behavior reviewed through `2bdabcb`.

This branch is `mycode-go-wails`. It adds the Wails desktop adapter on top of `mycode-go` and owns only desktop-specific entry points, build files, and the web transport adapter.

## Project Layout

```text
./
  agent/                    # agent loop and compact replay
  attachment/               # file/text/bytes attachments -> message blocks
  message/                  # canonical block-based message model
  tools/                    # the 4 built-in tools and execution; keep implementation in tools.go
  session/                  # append-only JSONL store and rewind
  provider/                 # provider adapters
    anthropic.go            # anthropic, moonshotai, minimax
    google.go               # google
    openai_responses.go     # openai
    openai_chat.go          # openai_chat, deepseek, zai, openrouter
    models.go               # bundled model metadata catalog and resolution

  cmd/mycode-go/              # CLI: run, web, session list, --version
  internal/core/              # transport-agnostic service layer, RunManager, workspace browser
  internal/permissions/       # CLI/web tool permission policy
  internal/config/            # layered config loading and provider resolution
  internal/prompt/            # system prompt, AGENTS discovery, skills discovery
  internal/server/            # HTTP adapter and SSE framing

  app.go                      # Wails bindings and runtime event bridge
  main.go                     # Wails desktop entry point
  wails.json                  # Wails project config

web/src/                      # shared React + Vite UI from Python main
  hooks/useChat.ts            # chat state + SSE/Wails streaming
  utils/messages.ts           # canonical blocks → UI messages
  utils/transport.ts          # HTTP/Wails transport selection

scripts/
  update_models_catalog.py    # regenerates provider/models_catalog.json
  sync_web_dist.sh            # copies web/dist into Go's embedded webdist directory
  build_wails_frontend.sh     # builds web/ and copies assets for Wails embed
```

## Message Model

A single block-based JSON format is used at runtime, in persistence, and over the API. Block types: `text` · `image` · `document` · `thinking` · `tool_use` · `tool_result`.

`thinking` blocks are first-class session data. Tool results are stored as `user` messages whose `tool_result` blocks carry provider-facing `output` plus structured UI `metadata`. Session `meta.json` stores `cwd`, `title`, `created_at`, and `updated_at`; provider/model/api_base live on per-turn messages.

Cancelled provider streams may persist partial assistant `thinking`/`text`. Cancelled streaming tools append `error: cancelled` to emitted output.

Full schema, JSONL record types, replay rules, compact, and rewind behavior live in `docs/sessions.md`.

## Agent Loop

Per user turn (`agent/agent.go`):

1. Append the user message.
2. Stream one provider turn through `applyCompactReplay`.
3. Persist the assistant message. If the provider stream is cancelled after deltas arrive, persist a partial assistant message before emitting `error: cancelled`.
4. Execute local tool calls.
5. Append a `user` tool-result message. If a streaming bash tool is cancelled, the final tool result includes already emitted output followed by `error: cancelled`.
6. Repeat until there are no tool calls.
7. Optionally compact when usage crosses `compact_threshold`; the compact event is persisted inline and projected on the next provider call.

## Provider Adapters

Adapter ids: `anthropic`, `moonshotai`, `minimax`, `google`, `openai`, `openai_chat`, `deepseek`, `zai`, `openrouter`. Provider wire-format quirks, reasoning replay, image/PDF serialization, and env vars live in `docs/providers.md`.

## SSE Contract

`GET /api/runs/{run_id}/stream` event types: `reasoning`, `reasoning_done`, `text`, `tool_start`, `tool_output`, `tool_done`, `compact`, `error`, `permission_request`, `permission_resolved`, `usage`. Every event carries a monotonically increasing `seq`.

Event names and payload shapes are a cross-component contract. Changes need to land in server, desktop bridge, and web UI together. Full payload fields and reconnect semantics live in `docs/api.md`.

Wails live run updates use runtime event `mycode:run_event`. The event payload is `{run_id, session_id, event}`. The nested `event` follows the SSE event contract, plus `type="done"` when a run finishes.

## Detailed Specs

Read the relevant doc before related changes.

| Area                                                                 | Doc                                                |
| -------------------------------------------------------------------- | -------------------------------------------------- |
| Public SDK API (`agent.Config`, `Chat`, model capabilities)          | `docs/sdk.md`                                      |
| `agent`, `attachment`, `message`, `tools`, `session`                 | `docs/sessions.md`                                 |
| `provider/*`                                                         | `docs/providers.md`                                |
| `internal/core`, `internal/server`, SSE events, or routes            | `docs/api.md`                                      |
| `internal/config`, `internal/prompt`, `internal/permissions`, models | `docs/config.md`                                   |
| `web/src/**`                                                         | `docs/web.md`                                      |
| Wails entry points, bindings, desktop build, or desktop transport    | `docs/wails.md`                                    |
| Cross-cutting contract changes                                       | `docs/api.md` + `docs/sessions.md` + `docs/web.md` |

For third-party SDKs and APIs touched by adapter or runtime code, prefer `context7` lookups over assumptions.

## Interfaces

CLI commands: `mycode-go <message>`, `mycode-go run "..."`, `mycode-go web [--dev]`, `mycode-go session list`. This Go rewrite does not include the Python terminal TUI.

Server routes are mounted under `/api`: chat runs, run stream/cancel/decide, config, settings, sessions, and workspaces. Endpoint schemas and run lifecycle details live in `docs/api.md`.

Desktop bindings:

- `GetConfig`, `Settings`, `UpdateSettings`
- `ListSessions`, `LoadSession`, `DeleteSession`, `ClearSession`
- `StartChat`, `CancelRun`, `DecideRun`
- `SelectFiles`
- `WorkspaceRoots`, `BrowseWorkspace`

## Commit Conventions

Format: `type(scope): description`.

Scopes:

- `web` — changes under `web/` only
- `backend` — Go backend changes only
- `cli` — CLI changes only
- `wails` — desktop entry points, Wails config, and desktop build workflow
- `docs` — documentation only

## Dev Workflow

Make targets agents may use:

- `make dev` — full-stack dev server.
- `make web-check` — web checks.
- `make web-build` — web build and sync.
- `make check` — full verification.
- `make build` — embedded binary.

Raw commands:

```bash
go test ./...
go vet ./...
go test -race ./...
golangci-lint fmt ./...
golangci-lint run ./...
go run ./cmd/mycode-go web --dev

pnpm --dir web install
pnpm --dir web typecheck
pnpm --dir web test:run
pnpm --dir web dev
pnpm --dir web build
./scripts/sync_web_dist.sh
```

Desktop:

```bash
make wails-install
make wails-dev
make wails-build
```

Update bundled model metadata with:

```bash
uv run --no-project python ./scripts/update_models_catalog.py
```
