# mycode — Agent Context

Always-loaded context for agent runs on this project. Detailed specs live in `docs/`; this file points at them rather than duplicating their content.

## Product

`mycode` is a minimal coding agent shipped as two PyPI packages:

- `mycode-sdk` (import `mycode`) — the runtime: agent loop, message format, session store, provider adapters, and built-in tools. Lightweight, suitable for embedding the agent in other Python apps.
- `mycode-cli` (import `mycode_cli`) — the interactive CLI and FastAPI web server built on top of the SDK.

The web UI lives in a separate repo, [`legibet/mycode-web`](https://github.com/legibet/mycode-web), included as the `web/` git submodule. Develop UI there; this repo only builds it into the packaged static assets.

## Project Layout

```text
mycode/src/mycode/        # SDK package
  agent.py                # agent loop (Agent, achat, run)
  messages.py             # internal block-based message format
  tools.py                # ToolSpec, ToolExecutor, @tool, the 4 built-in tools
  hooks.py                # before_tool / after_tool hook protocol
  session.py              # append-only JSONL store, compact, rewind
  models.py               # bundled model metadata lookup
  models_catalog.json     # generated; source: scripts/update_models_catalog.py
  utils.py                # small typed helpers
  providers/              # one file per protocol family
    base.py               # ProviderAdapter ABC + prepare_messages()
    anthropic_like.py     # anthropic, moonshotai, minimax
    gemini.py             # google
    openai_responses.py   # openai
    openai_chat.py        # openai_chat, deepseek, zai, openrouter

cli/src/mycode_cli/       # CLI + FastAPI web server
  main.py                 # Typer entrypoint, slash commands, session resolution
  runtime.py              # build_agent() shared by TUI and server
  config.py               # layered JSON config, config validation, provider resolution, paths
  permissions.py          # tool permission policy + before_tool hook
  system_prompt.py        # base prompt + AGENTS.md + skills discovery
  tui/                    # interactive terminal chat (chat.py, render.py, theme.py)
  server/                 # FastAPI app, routers, run_manager, schemas; settings router validates config writes

web/                      # React + Vite UI (git submodule: legibet/mycode-web)
  hooks/useChat.ts        # chat state + SSE streaming
  utils/messages.ts       # buildRenderMessages(): canonical blocks → UI messages

scripts/
  update_models_catalog.py  # regenerates mycode/src/mycode/models_catalog.json
  release.sh                # bumps versions + builds wheels for both packages
```

## Internal Message Model

A single block-based JSON format is used at runtime and persisted to JSONL. Block types: `text` · `image` · `thinking` · `tool_use` · `tool_result`. Persisted message roles: `user` · `assistant` · `compact` · `rewind`; the last two are inline timeline markers (`compact` carries the summary text and stays visible to UIs; `rewind` carries an index into the visible list and is consumed at load time).

`thinking` blocks are first-class session data — persisted, replayed to providers, and shown in UI. Provider-specific extras live in `meta.native` on messages and `block.meta.native` on blocks. Tool results are stored as `user` messages whose `tool_result` blocks carry the replayed `output` plus structured UI `metadata`. Compact substitution (replacing pre-compact history with a summary continuation) happens lazily in the provider adapter's `prepare_messages` (via `compact.apply_compact_replay`) per request; visible state and JSONL keep the real history.

Cancelled provider streams may persist partial assistant `thinking`/`text`. Cancelled streaming tools append `error: cancelled` to emitted output.

Full schema, JSONL record types, replay rules, and the rewind/compact projection live in `docs/sessions.md`. The SDK-level event surface and `Agent` API live in `docs/sdk.md`.

## Agent Loop

Per user turn (`mycode/src/mycode/agent.py`):

1. Append user message to session.
2. Call provider adapter → stream events to CLI/server.
3. Persist assistant message to JSONL.
4. Execute tool calls locally.
5. Append `user` tool-result message.
6. Repeat until no tool calls; `max_turns` defaults to unlimited.
7. After each assistant/tool-result boundary, optionally compact when `total_tokens ≥ context_window × compact_threshold` (default `0.8`).

## Provider Adapters

Adapter ids: `anthropic`, `moonshotai`, `minimax`, `google`, `openai`, `openai_chat`, `deepseek`, `zai`, `openrouter`. All implement `ProviderAdapter.stream_turn()`; canonical → wire-format projection lives in `prepare_messages()`.

Per-adapter SDK, base URL, env vars, reasoning effort mapping, image/PDF serialization, and replay quirks live in `docs/providers.md`. Most adapter regressions come from missing replay shapes (native thought signatures, empty `reasoning_content` markers, function-call id matching).

## SSE Contract

`GET /api/runs/{run_id}/stream` event types: `reasoning`, `reasoning_done`, `text`, `tool_start`, `tool_output`, `tool_done`, `compact`, `error`, `permission_request`, `permission_resolved`, `usage`. Every event also carries a monotonically increasing `seq: int`.

Event names and payload shapes are a cross-component contract — changes need to land in server, CLI, and web UI together. Full payload fields, reconnect semantics (`after=<seq>`), and the permission request/resolve flow live in `docs/api.md`. SDK-level event variants (used by SDK embedders) live in `docs/sdk.md`.

## Web UI

`web/` is the `legibet/mycode-web` submodule; UI internals (components, state model, streaming, config) live in `web/AGENTS.md`.

- `mycode web` — serves packaged assets from `cli/src/mycode_cli/server/static/`; missing at startup → API-only with a warning.
- `mycode web --dev` — API-only (pair with `pnpm --dir web dev`); CORS allows only the Vite dev origin.
- `uv build --package mycode-cli` — builds web and packages `static/` via `cli/hatch_build.py` (editable installs skip it).

## Detailed Specs

Read the relevant doc before related changes.

| Area                                                                            | Doc                                             |
| ------------------------------------------------------------------------------- | ----------------------------------------------- |
| `mycode/src/mycode/agent.py`, `messages.py`, `tools.py`, `hooks.py`, public SDK | `docs/sdk.md`                                   |
| `mycode/src/mycode/session.py` or anything touching JSONL / compact / rewind    | `docs/sessions.md`                              |
| `mycode/src/mycode/providers/*`                                                 | `docs/providers.md`                             |
| `cli/src/mycode_cli/server/**` or any SSE event / route                         | `docs/api.md`                                   |
| `cli/src/mycode_cli/config.py`, `system_prompt.py`, `permissions.py`            | `docs/config.md`                                |
| `web/src/**`                                                                    | `web/AGENTS.md`                                 |
| Cross-cutting changes (e.g. a new SSE event)                                    | `docs/api.md` + `docs/sdk.md` + `web/AGENTS.md` |

## Interfaces

CLI commands: `mycode` (interactive), `mycode run "..."` (non-interactive), `mycode web [--dev]`, `mycode session list`. Inside the TUI: `@path` attaches files (text → `<file>` snapshots, images/PDFs → structured blocks); slash commands `/clear` `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/q`.

Server routes are mounted under `/api`: chat (`/api/chat`, `/api/runs/...`), sessions, settings, workspaces, config. Endpoint schemas, error codes, and the run manager's lifecycle live in `docs/api.md`.

`mycode web` serves packaged assets without CORS. `mycode web --dev` allows only Vite dev origins.

## Commit Conventions

Format: `type(scope): description`.

Scopes:

- `web` — `web/` submodule pointer bumps
- `sdk` — SDK package (`mycode/`) only
- `cli` — CLI/server package (`cli/`) only

Examples:

```text
chore(web): bump mycode-web
fix(sdk): handle empty tool result in compact
feat(sdk): add tool decorator
refactor(cli): unify provider switcher
docs: update SSE contract in AGENTS.md
```

## Dev Workflow

```bash
git submodule update --init                            # fetch web/ (legibet/mycode-web)
uv sync --dev                                          # install/update Python deps
pnpm --dir web install                                 # install web deps

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
just setup                                             # init submodule + install all deps
just dev                                               # backend API + Vite dev together
just check                                             # ruff check, basedpyright
just test                                              # Python tests
just fmt                                               # ruff fix/format
```

Releases are cut by `scripts/release.sh`, which bumps the `mycode-sdk` and `mycode-cli` versions in their `pyproject.toml`, refreshes the CLI's pin on `mycode-sdk`, builds both wheels, and tags the repo.

The bundled model metadata catalog (`mycode/src/mycode/models_catalog.json`) is regenerated by:

```bash
uv run python scripts/update_models_catalog.py
```
