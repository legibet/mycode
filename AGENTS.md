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

## 3) Current Architecture (Post-Refactor)

### Backend (FastAPI)

- `app/main.py`
  - Creates FastAPI app.
  - Mounts API routers under `/api`.
  - Serves frontend static files from `frontend/dist` when built.

- `app/routers/chat.py`
  - `POST /api/chat` (SSE stream)
  - `POST /api/cancel`
  - `GET /api/config`
  - Resolves effective config per-request from `~/.mycode/config.json` and `workspace/.mycode/config.json`.
  - Creates `Agent` per request using persisted session messages.
  - Persists new messages incrementally via callback.

- `app/routers/sessions.py`
  - `POST /api/sessions`
  - `GET /api/sessions`
  - `GET /api/sessions/{id}`
  - `DELETE /api/sessions/{id}`
  - `POST /api/sessions/{id}/clear`

- `app/routers/workspaces.py`
  - Workspace browsing endpoints.
  - Roots from `MYCODE_WORKSPACE_ROOTS` or `WORKSPACE_ROOTS`.

- `app/config.py`
  - Loads layered config from `~/.mycode/config.json` and `workspace/.mycode/config.json` only.
  - Uses project/workspace-local config to override global defaults.
  - Merges provider settings with workspace-local precedence.
  - Exposes workspace root / config path metadata for runtime consumers.

### Agent Runtime

- `app/agent/core.py`
  - Minimal streaming agent loop with tool calls.
  - Uses `any_llm.acompletion`.
  - Aggregates streamed tool calls by `delta.tool_calls[].index`.
  - Persists user/assistant/tool messages (system prompt is runtime-only).
  - Handles interrupted previous tool-calls with synthetic tool errors.
  - Uses shared persistence helpers to keep message writes consistent and reduce loop duplication.
  - Injects hierarchical AGENTS.md-style instructions and discovered skills into the runtime system prompt.
  - Supports active cancellation while a `bash` tool call is running (kills subprocesses and returns `error: cancelled`).

- `app/agent/instructions.py`
  - Discovers AGENTS.md from `~/.mycode/AGENTS.md` with `~/.agents/AGENTS.md` as a compatibility fallback.
  - Loads project instructions only from `workspace_root/AGENTS.md`.
  - At global scope, `~/.mycode/AGENTS.md` takes precedence over `~/.agents/AGENTS.md`.
  - Truncates injected instruction bytes with a fixed runtime limit.

- `app/agent/skills.py`
  - Discovers `SKILL.md` files from global `~/.mycode/skills/`, `~/.agents/skills/`, plus project-level `.mycode/skills/` and `.agents/skills/` under the workspace root.
  - Parses YAML frontmatter (name, description), validates, and deduplicates.
  - Produces `<available_skills>` XML block for system prompt injection (progressive disclosure).
  - Project-level skills override global skills of the same name.
  - At the same scope, `.mycode` takes precedence over `.agents`.
  - Model uses existing `read` tool to load full skill content on demand.

- `app/agent/tools.py`
  - Defines OpenAI-compatible tool schemas (`TOOLS`).
  - Implements `ToolExecutor` for `read/write/edit/bash`.
  - Includes truncation limits and large-output handling.
  - `bash` spills very large output to `tool-output/bash-<tool_call_id>.log` once memory threshold is exceeded, while still streaming lines.
  - `edit` uses exact-match first; if not found, it applies a conservative fuzzy fallback (line-ending + trailing-whitespace normalization only, unique match required), then returns closest-line hints when still unmatched.

- `app/agent/system_prompt.md`
  - Canonical prompt guidance.
  - Explicitly instructs model to use `bash + rg` for search.

### Session Storage

- `app/session.py`
  - Append-only JSONL message log (instead of SQLite full blob overwrite).
  - Per-session directory layout:

```
app/data/sessions/<session_id>/
  meta.json
  messages.jsonl
  tool-output/
```

- `messages.jsonl` stores OpenAI-style message objects:
  - user
  - assistant (optional `tool_calls`)
  - tool

---

## 4) Frontend Architecture

- React + Vite (`frontend/`)
- Styling: Tailwind CSS 3 with CSS custom properties (HSL tokens)
- Design system: **Terminal-Luxe Deep Ocean** — dark-first, minimal, content-focused

### Design Language

- **Fonts**: Satoshi (body/sans), DM Mono (display/code/labels) — loaded via Google Fonts
- **Color scheme (dark default)**:
  - Background: `#0A1628` (deep navy), Foreground: `#D4DEE8` (cool silver)
  - Accent: `#5BA4CF` (ice blue) — used for focus states, active indicators, send button, streaming cursor
  - Sidebar: `#060F1E` (deeper navy layer)
- **Color scheme (light)**:
  - Background: `#F3F6FA` (pale frost), Accent: `#297AB0` (deeper blue)
- **Theme switching**: `:root` = dark (default), `.light` class = light mode. No `.dark` class needed.
- **Key visual patterns**:
  - Messages use left-border role indicators (2px line) instead of avatar circles
  - Role labels are monospace uppercase (`you` / `assistant`)
  - ToolCard collapsed state: tool name + args preview + status dot (green/amber/red)
  - ToolCard running state: thin progress bar animation
  - Empty state: `mycode` with blinking cursor + "ready."
  - Input area: accent focus line at bottom, gradient fade mask above
  - Sidebar: 240px wide, compact, dashed new-chat button
  - Messages animate in with `fade-in-up`, streaming shows blinking cursor

### Core Chat Logic

- `frontend/src/hooks/useChat.js`
  - Streams SSE from `/api/chat`
  - Applies events (`text`, `tool_start`, `tool_output`, `tool_done`, `error`)
  - Manages session CRUD calls
- `frontend/src/utils/messages.js`
  - Transforms provider message format into UI message parts

### Component Structure

```
src/components/
  Layout.jsx              — root surface with theme transition
  ThemeProvider.jsx        — dark-first theme context (system/light/dark)
  Sidebar.jsx             — session list + settings (history/appearance/workspace/provider)
  WorkspacePicker.jsx     — modal workspace browser
  Chat/
    MessageList.jsx       — scrollable list + empty state
    MessageBubble.jsx     — left-border role indicator + content parts
    MarkdownBlock.jsx     — GFM markdown renderer
    CodeBlock.jsx         — syntax-highlighted code with copy button
    ToolCard.jsx          — collapsible tool execution card
    InputArea.jsx         — floating input with accent focus line
  UI/
    Button.jsx            — variant button component
    Input.jsx             — styled input component
```

Frontend expects backend event contract to remain stable.

---

## 5) Event Contract (SSE)

Current stream event types used by UI:

- `text` → assistant text delta
- `tool_start` → tool call started (`id`, `name`, `args`)
- `tool_output` → incremental tool output (mainly bash)
- `tool_done` → final tool result (`result`)
- `error` → error message

Do not break these types without coordinated frontend updates.

---

## 6) Key Technical Decisions

1. **No global `os.chdir()` in request path**
   - Tools execute with explicit `cwd` context.
   - Avoids cross-request cwd contamination in async server mode.

2. **Config and instruction loading are workspace-aware**
   - Config stays intentionally minimal: only `~/.mycode/config.json` and `workspace/.mycode/config.json`.
   - `~/.agents/` remains a compatibility source for instructions and skills only.
   - At the same scope, native `.mycode` content overrides compatible `.agents` content.

3. **Append-only session writes**
   - Better reliability than rewriting full conversation state.
   - Better crash behavior and future compaction compatibility.

4. **Truncation-first tool outputs**
   - Keep context lean.
   - For large bash outputs, store full output in `tool-output/` and return actionable pointer.
   - For very large live outputs, switch from in-memory buffering to spill-to-file mode.

5. **Deterministic edit semantics with conservative fallback**
   - `edit` still prefers exact unique matches.
   - If exact match fails, only line-ending/trailing-whitespace normalization is allowed.
   - Fuzzy fallback must still resolve to a unique match; otherwise it fails.
   - If no match exists, return a closest-line hint to speed up retry.

6. **Tool cancellation semantics**
   - Cancelling during `bash` actively terminates subprocesses.
   - Agent records a deterministic `error: cancelled` tool result.

---

## 7) Development Conventions

- Python runtime/deps: **uv only**.
- Keep modules small, typed, and documented.

### Common commands

```bash
# Backend
uv run uvicorn app.main:app --reload --port 8000

# CLI
uv run python cli.py

# Basic syntax check
uv run python -m py_compile $(find app -name "*.py" -type f)

# Frontend
cd frontend && npm install && npm run build

# Tests
uv run pytest tests/ -v
uv run pytest tests/test_session.py -v
uv run pytest tests/test_tools.py -v
```

---

## 8) Guardrails for Future Refactors

If you change architecture, preserve these invariants unless explicitly requested:

- 4-tool core remains unchanged.
- SSE event contract remains compatible.
- Session persistence remains append-only and human-inspectable.
- System prompt remains concise and operationally explicit.
- Search guidance remains `bash + rg`, not additional built-in search tools.

When in doubt, choose the simpler design.
