# AGENTS.md — mycode

This file is the **authoritative project context** for future agent runs. Automatically update this file with any architectural changes, new design decisions, or shifts in product vision. The agent will read this file at the start of each run to understand the current state of the codebase and project goals.

## 1) Product Vision

mycode is a **personal minimal coding agent** inspired by [pi](https://github.com/badlogic/pi-mono)'s design philosophy:

- Small, stable core.
- Minimal built-in primitives.
- Clear agent loop.
- Low token overhead.
- Extensibility comes later (skills), not via core complexity.

Current scope is intentionally focused on a robust core + web/CLI usability.

---

## 2) Non-Negotiable Core Principles

1. **Only 4 built-in tools** are exposed to the model:
   - `read`
   - `write`
   - `edit`
   - `bash`
2. Do **not** add `grep`, `glob`, or other search tools to the core.
   - Search should be done via `bash` (`rg`, `find`, etc.).
3. Keep agent behavior concise and deterministic.
4. Keep token usage low (truncate tool outputs, avoid noisy prompts).
5. Prefer simple, readable Python over framework-heavy abstractions.

---

## 3) Project Structure

```
mycode/                        # Python package
  core/                        # Core runtime (single source of truth)
    agent.py                   # Agent class + streaming agent loop
    tools.py                   # 4-tool schemas + ToolExecutor
    config.py                  # Settings, ProviderConfig, resolve_provider
    session.py                 # SessionStore (append-only JSONL)
    instructions.py            # AGENTS.md discovery + injection
    skills.py                  # Skill discovery + prompt formatting
    system_prompt.md           # Canonical system prompt
  server/                      # Interface: FastAPI API server
    app.py                     # create_app(), mounts routers + frontend
    schemas.py                 # Pydantic request/response models
    deps.py                    # Shared dependencies (SessionStore instance)
    routers/
      chat.py                  # POST /api/chat (SSE), POST /api/cancel, GET /api/config
      sessions.py              # CRUD for sessions
      workspaces.py            # Workspace browsing
  frontend/                    # Interface: React + Vite web UI
    src/
    package.json
    vite.config.js
  cli.py                       # Interface: CLI/TUI

tests/
pyproject.toml
AGENTS.md → CLAUDE.md
```

### Key design: core as single source

`mycode.core` contains all runtime logic. Both `mycode.server` and `mycode.cli` are thin interface layers that import from core. Provider resolution (`resolve_provider`), session storage, agent construction — all live in core.

---

## 4) Core Modules

- `mycode.core.agent`
  - Minimal streaming agent loop with tool calls.
  - Uses `any_llm.acompletion`.
  - Aggregates streamed tool calls by `delta.tool_calls[].index`.
  - Persists user/assistant/tool messages (system prompt is runtime-only).
  - Streams provider reasoning/thinking as transient UI events only; reasoning is not persisted into session history.
  - Handles interrupted previous tool-calls with synthetic tool errors.
  - Injects hierarchical AGENTS.md-style instructions and discovered skills into the runtime system prompt.
  - Supports active cancellation while a `bash` tool call is running (kills subprocesses and returns `error: cancelled`).

- `mycode.core.config`
  - Loads layered config from `~/.mycode/config.json` and `workspace/.mycode/config.json` only.
  - Uses project/workspace-local config to override global defaults.
  - Does not auto-load `.env` files.
  - LLM `provider` / `model` / `base_url` come from explicit request args or layered config only; standard API key env vars are runtime-only overrides.
  - `resolve_provider()` — shared by CLI and server, eliminates duplicated resolution logic.
  - Exposes workspace root / config path metadata for runtime consumers.

- `mycode.core.tools`
  - Defines OpenAI-compatible tool schemas (`TOOLS`).
  - Implements `ToolExecutor` for `read/write/edit/bash`.
  - Includes truncation limits and large-output handling.
  - `bash` spills very large output to `tool-output/bash-<tool_call_id>.log` once memory threshold is exceeded, while still streaming lines.
  - `edit` uses exact-match first; if not found, it applies a conservative fuzzy fallback (line-ending + trailing-whitespace normalization only, unique match required), then returns closest-line hints when still unmatched.

- `mycode.core.session`
  - Append-only JSONL message log.
  - Per-session directory layout:

```
mycode/data/sessions/<session_id>/
  meta.json
  messages.jsonl
  tool-output/
```

- `mycode.core.instructions`
  - Discovers AGENTS.md from `~/.mycode/AGENTS.md` with `~/.agents/AGENTS.md` as a compatibility fallback.
  - Loads project instructions only from `workspace_root/AGENTS.md`.
  - Truncates injected instruction bytes with a fixed runtime limit.

- `mycode.core.skills`
  - Discovers `SKILL.md` files from global `~/.mycode/skills/`, `~/.agents/skills/`, plus project-level `.mycode/skills/` and `.agents/skills/` under the workspace root.
  - Parses YAML frontmatter (name, description), validates, and deduplicates.
  - Produces `<available_skills>` XML block for system prompt injection (progressive disclosure).

---

## 5) Interface Layers

### Server (`mycode.server`)

- `app.py` — FastAPI app, mounts API routers under `/api`, serves frontend static files from `mycode/frontend/dist`.
- `deps.py` — shared `SessionStore` instance (single instance for all routers).
- `routers/chat.py` — SSE streaming chat, cancel, config endpoints.
- `routers/sessions.py` — session CRUD.
- `routers/workspaces.py` — workspace browsing.
- `schemas.py` — Pydantic models for API requests/responses.

### CLI (`mycode.cli`)

- Interactive REPL with rich markdown rendering.
- Single-shot mode (`--once`).
- CLI session semantics: default launch creates a new session; resuming prior context is explicit via `--continue` or `--session <id>`.
- When resuming in interactive CLI, show current session identity and a short history preview so restored context is visible to the user.
- CLI session management stays minimal but explicit: `mycode session list` for discovery, `/resume` for switching to a saved workspace session, `/new` for starting a fresh session without leaving the TUI.
- Uses `resolve_provider()` from core for provider/model resolution.

### Frontend (`mycode/frontend`)

- React + Vite
- Styling: Tailwind CSS 3 with CSS custom properties (HSL tokens)
- Design system: **Terminal-Luxe Deep Ocean** — dark-first, minimal, content-focused
- Core hook: `useChat.js` — streams SSE from `/api/chat`

---

## 6) Event Contract (SSE)

Current stream event types used by UI:

- `reasoning` → assistant reasoning/thinking delta (stream-only, not persisted into session history)
- `text` → assistant text delta
- `tool_start` → tool call started (`id`, `name`, `args`)
- `tool_output` → incremental tool output (mainly bash)
- `tool_done` → final tool result (`result`)
- `error` → error message

Do not break these types without coordinated frontend updates.

---

## 7) Key Technical Decisions

1. **No global `os.chdir()` in request path** — tools execute with explicit `cwd` context.
2. **Config and instruction loading are workspace-aware** — `~/.agents/` remains a compatibility source for instructions and skills only.
3. **LLM config precedence is explicit > env API key > project config > global config** — environment variables do not define model/provider/base_url.
4. **Append-only session writes** — better reliability and crash behavior.
5. **Truncation-first tool outputs** — keep context lean.
6. **Deterministic edit semantics with conservative fallback** — exact match preferred, fuzzy only for whitespace/line-ending differences.
7. **Tool cancellation semantics** — cancelling during `bash` actively terminates subprocesses.
8. **Shared provider resolution** — `resolve_provider()` in core eliminates duplication between CLI and server.
9. **Explicit CLI session resume** — interactive CLI should not silently reuse hidden prior context; resume must be user-selected and visibly indicated.

---

## 8) Development Conventions

- Python runtime/deps: **uv only**.
- Keep modules small, typed, and documented.

### Common commands

```bash
# Server
uv run uvicorn mycode.server.app:app --reload --port 8000

# CLI
uv run mycode
# or: uv run python -m mycode.cli

# Syntax check
uv run python -m py_compile $(find mycode -name "*.py" -type f)

# Frontend
cd mycode/frontend && pnpm install && pnpm run build

# Tests
uv run python -m pytest tests/ -v
```

---

## 9) Guardrails for Future Refactors

If you change architecture, preserve these invariants unless explicitly requested:

- 4-tool core remains unchanged.
- SSE event contract remains compatible.
- Session persistence remains append-only and human-inspectable.
- System prompt remains concise and operationally explicit.
- Search guidance remains `bash + rg`, not additional built-in search tools.
- Core as single source — interfaces import from `mycode.core`, never the reverse.

When in doubt, choose the simpler design.
