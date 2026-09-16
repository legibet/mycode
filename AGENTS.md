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
    openai_responses.py   # openai, xai
    openai_chat.py        # alibaba, openai_chat, deepseek, zai, openrouter

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

## Message Model

One block-based JSON format is used at runtime and persisted to JSONL. Block types: `text` · `image` · `thinking` · `tool_use` · `tool_result`. Persisted roles: `user` · `assistant`, plus inline `compact` and `rewind` markers. Tool results persist as `user` messages carrying `tool_result` blocks; provider-specific extras live in `meta.native`. `thinking` blocks are first-class session data — persisted, replayed to providers, and shown in UI. The provider adapter substitutes post-compact history lazily per request (`prepare_messages`); visible state and JSONL keep the real history. Schema, record types, and replay rules: `docs/sessions.md`.

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

All adapters implement `ProviderAdapter.stream_turn()`; canonical → wire-format projection lives in `prepare_messages()`. Adapter ids, env vars, effort mapping, and image/PDF serialization: `docs/providers.md`. Most adapter regressions come from missing replay shapes (native thought signatures, empty `reasoning_content` markers, function-call id matching).

## SSE Contract

Runs stream over `GET /api/runs/{run_id}/stream`; every event carries a monotonically increasing `seq`. Event names and payload shapes are a cross-component contract — changes must land in server, CLI, and web UI together. Full contract: `docs/api.md`; SDK event variants: `docs/sdk.md`.

## Detailed Specs

Read the relevant doc before related changes.

| Area                                                                                                         | Doc                                           |
| ------------------------------------------------------------------------------------------------------------ | --------------------------------------------- |
| `mycode/src/mycode/agent.py`, `messages.py`, `tools.py`, `hooks.py`, `models.py`                             | `docs/sdk.md`                                 |
| `mycode/src/mycode/session.py`, `cli/src/mycode_cli/sessions.py`, anything touching JSONL / compact / rewind | `docs/sessions.md`                            |
| `mycode/src/mycode/providers/*`                                                                              | `docs/providers.md`                           |
| `cli/src/mycode_cli/tools.py`, `web_tools.py`, `permissions.py`                                              | `docs/tools.md`                               |
| `cli/src/mycode_cli/server/**` or any SSE event / route                                                      | `docs/api.md`                                 |
| `cli/src/mycode_cli/config.py`                                                                               | `docs/config.md`                              |
| `cli/src/mycode_cli/system_prompt.py` (skills / instructions discovery)                                      | `cli/README.md`                               |
| `web/**`                                                                                                     | `docs/web.md`                                 |
| Cross-cutting changes (e.g. a new SSE event)                                                                 | `docs/api.md` + `docs/sdk.md` + `docs/web.md` |

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
just dev                                               # backend API + Vite dev together
just check                                             # ruff check, basedpyright, web typecheck, biome check
just test                                              # Python + web tests
just fmt                                               # ruff fix/format + biome check --write
```

Releases are cut by `scripts/release.sh`, which bumps the `mycode-sdk` and `mycode-cli` versions in their `pyproject.toml`, refreshes the CLI's pin on `mycode-sdk`, builds both wheels, and tags the repo.

Regenerate `mycode/src/mycode/models_catalog.json` with:

```bash
uv run python scripts/update_models_catalog.py
```
