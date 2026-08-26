# mycode — Agent Context

Always-loaded context for agent runs on this project. Detailed specs live in `docs/`.

## Product

`mycode` is a minimal coding agent shipped as two PyPI packages:

- `mycode-sdk` (import `mycode`) — the runtime: agent loop, message format, session store, provider adapters, and the tool runtime. Lightweight, suitable for embedding the agent in other Python apps.
- `mycode-cli` (import `mycode_cli`) — the interactive CLI and FastAPI web server built on top of the SDK, including local file/shell tools and configurable web access.

## Project Layout

```text
mycode/src/mycode/        # SDK package
  agent.py                # agent loop (Agent, achat, run)
  messages.py             # internal block-based message format
  tools.py                # ToolSpec, ToolExecutor, ToolContext, @tool
  hooks.py                # before_tool / after_tool hook protocol
  session.py              # append-only JSONL timeline and rewind replay
  models.py               # bundled model metadata lookup
  models_catalog.json     # generated; source: scripts/update_models_catalog.py
  providers/              # one file per protocol family
    base.py               # ProviderAdapter ABC + prepare_messages()
    anthropic_like.py     # anthropic, moonshotai, minimax
    gemini.py             # google, google_vertex
    openai_responses.py   # openai
    openai_chat.py        # alibaba, openai_chat, deepseek, zai, openrouter, xai

cli/src/mycode_cli/       # CLI + FastAPI web server
  main.py                 # Typer entrypoint, slash commands, session resolution
  runtime.py              # build_agent() shared by TUI and server
  sessions.py             # CLI session catalog and lifecycle
  workspace.py            # CLI workspace/tool dependency context
  tools.py                # local tools (read, write, edit, bash)
  web_tools.py            # configurable webfetch / websearch tools
  config.py               # layered JSON config, config validation, provider resolution, paths
  permissions.py          # tool permission policy + before_tool hook
  system_prompt.py        # base prompt + AGENTS.md + skills discovery
  tui/                    # interactive terminal chat (chat.py, render.py, theme.py)
  server/                 # FastAPI app, routers, run_manager, schemas; settings router validates config writes

web/src/                  # React + Vite UI
  hooks/useChat.ts        # chat state + SSE streaming
  utils/messages.ts       # buildRenderMessages(): canonical blocks → UI messages

scripts/
  update_models_catalog.py  # regenerates mycode/src/mycode/models_catalog.json
  release.sh                # bumps versions + builds wheels for both packages
```

## Internal Message Model

A single block-based JSON format is used at runtime and persisted to JSONL. Block types: `text` · `image` · `thinking` · `tool_use` · `tool_result`. Persisted message roles: `user` · `assistant` · `compact` · `rewind`; the last two are inline timeline markers (`compact` carries the summary text and stays visible to UIs; `rewind` carries an index into the visible list and is consumed at load time).

`thinking` blocks are first-class session data — persisted, replayed to providers, and shown in UI. Provider-specific extras live in `meta.native` on messages and `block.meta.native` on blocks. Tool results are stored as `user` messages whose `tool_result` blocks carry the replayed `output` plus structured UI `metadata`. Compact substitution (replacing pre-compact history with a summary continuation) happens lazily in the provider adapter's `prepare_messages` (via `compact.apply_compact_replay`) per request; visible state and JSONL keep the real history.

Skill snapshots use text blocks with `meta.skill_snapshot=true`. Providers receive the snapshot; titles and UIs use the original user text.

Cancelled provider streams may persist partial assistant `thinking`/`text` with `meta.stop_reason="cancelled"`. Cancelled tool calls end in an error `tool_done`; Bash keeps its captured output and appends `error: cancelled`. Tool calls with canonical `length` or `unknown` stop reasons are rejected before execution; a canonical `error` response ends the turn. Malformed JSON arguments carry `meta.invalid_input=true`.

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

Adapter ids: `alibaba`, `anthropic`, `moonshotai`, `minimax`, `google`, `google_vertex`, `openai`, `openai_chat`, `deepseek`, `zai`, `openrouter`, `xai`. All implement `ProviderAdapter.stream_turn()`; canonical → wire-format projection lives in `prepare_messages()`.

Per-adapter SDK, base URL, env vars, reasoning effort mapping, image/PDF serialization, and replay quirks live in `docs/providers.md`. Most adapter regressions come from missing replay shapes (native thought signatures, empty `reasoning_content` markers, function-call id matching).

## SSE Contract

`GET /api/runs/{run_id}/stream` event types: `reasoning`, `reasoning_done`, `text`, `tool_start`, `tool_output`, `tool_done`, `compact`, `error`, `permission_request`, `permission_resolved`, `usage`. Every event also carries a monotonically increasing `seq: int`. `tool_output.output` is ordered, append-only display text; clients do not insert separators, and `[live output omitted]` marks a dropped middle segment.

Event names and payload shapes are a cross-component contract — changes need to land in server, CLI, and web UI together. Full payload fields, reconnect semantics (`after=<seq>`), and the permission request/resolve flow live in `docs/api.md`. SDK-level event variants (used by SDK embedders) live in `docs/sdk.md`.

## Detailed Specs

Read the relevant doc before related changes.

| Area                                                                            | Doc                                             |
| ------------------------------------------------------------------------------- | ----------------------------------------------- |
| `mycode/src/mycode/agent.py`, `messages.py`, `tools.py`, `hooks.py`, public SDK | `docs/sdk.md`                                   |
| SDK/CLI session storage or anything touching JSONL / compact / rewind           | `docs/sessions.md`                              |
| `mycode/src/mycode/providers/*`                                                 | `docs/providers.md`                             |
| `cli/src/mycode_cli/tools.py`, `web_tools.py`, or built-in tool output formats  | `docs/tools.md`                                 |
| `cli/src/mycode_cli/server/**` or any SSE event / route                         | `docs/api.md`                                   |
| `cli/src/mycode_cli/config.py`, `system_prompt.py`, `permissions.py`            | `docs/config.md`                                |
| `web/**`                                                                        | `docs/web.md`                                   |
| Cross-cutting changes (e.g. a new SSE event)                                    | `docs/api.md` + `docs/sdk.md` + `docs/web.md`   |

## Interfaces

CLI commands: `mycode` (interactive), `mycode run "..."` (non-interactive), `mycode web [--dev]`, `mycode session list`. Inside the TUI: `@path` attaches files (text → `<file>` snapshots, images/PDFs → structured blocks); built-in slash commands are `/clear` `/compact` `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/q`. A standalone `/<skill-name>` token references a discovered skill.

Server routes are mounted under `/api`: chat (`/api/chat`, `/api/runs/...`), sessions, settings, workspaces, config. Endpoint schemas, error codes, and the run manager's lifecycle live in `docs/api.md`.

`mycode web` serves packaged assets without CORS. `mycode web --dev` allows only Vite dev origins.

## Commit Conventions

Format: `type(scope): description`.

Scopes:

- `web` — changes under `web/` only
- `sdk` — SDK package (`mycode/`) only
- `cli` — CLI/server package (`cli/`) only

Examples:

```text
feat(web): add tool duration display
fix(sdk): handle empty tool result in compact
feat(sdk): add tool decorator
refactor(cli): unify provider switcher
docs: update SSE contract in AGENTS.md
```

## Dev Workflow

```bash
uv sync --dev                                          # install/update Python deps
pnpm --dir web install                                 # install web deps

uv run mycode                                          # start the CLI
uv run mycode web --dev                                # backend API for Vite dev
pnpm --dir web dev                                     # frontend Vite dev server

uv run basedpyright                                    # Python type checking
pnpm --dir web typecheck                               # web type checking
uv run pytest                                          # Python tests
pnpm --dir web test                                    # web tests

uv build --package mycode-sdk                          # build SDK package
uv build --package mycode-cli                          # build CLI package
```

Useful shortcuts:

```bash
just setup                                             # install all dependencies
just dev                                               # backend API + Vite dev together
just check                                             # ruff check, basedpyright, web typecheck, biome check
just test                                              # Python + web tests
just fmt                                               # ruff fix/format + biome check --write
```

Releases are cut by `scripts/release.sh`, which bumps the `mycode-sdk` and `mycode-cli` versions in their `pyproject.toml`, refreshes the CLI's pin on `mycode-sdk`, builds both wheels, and tags the repo.

The bundled model metadata catalog (`mycode/src/mycode/models_catalog.json`) is regenerated by:

```bash
uv run python scripts/update_models_catalog.py
```
