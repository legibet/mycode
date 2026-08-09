# Server API

Base prefix: `/api`. All endpoints are defined in `cli/src/mycode_cli/server/routers/`.

## Serving and CORS

`mycode web` serves packaged static assets and does not enable CORS. `mycode web --dev` starts the API-only app for Vite development and allows only `http://localhost:5173` and `http://127.0.0.1:5173`.

## Chat

### `POST /api/chat`

Start an agent run. Returns JSON immediately while the run streams asynchronously.

Request body (`ChatRequest`, `cli/src/mycode_cli/server/schemas.py`):

```json
{
  "message": "...",
  "input": null,
  "session_id": "default",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "cwd": "/path/to/workspace",
  "api_key": null,
  "api_base": null,
  "reasoning_effort": "medium",
  "rewind_to": null
}
```

Exactly one of `message` or `input` is required.

- `provider` — provider id or configured alias name
- `reasoning_effort` — request-level effort; omit the field, or send `null`/`"auto"`, to leave effort unspecified
- `rewind_to` — visible message index to rewind to before sending the new message; target must be a real user message
- A standalone `/<skill-name>` token adds the matching skill for `cwd`. The message prepends a hidden snapshot containing the frontmatter-free skill body, source path, and base directory, then keeps the original user text. Other slash tokens are sent as text.

Structured `input` uses `ChatInputBlock`:

```json
[
  {"type": "text", "text": "describe this"},
  {"type": "text", "text": "print('hi')", "name": "main.py", "is_attachment": true},
  {"type": "image", "path": "cat.png"},
  {"type": "image", "data": "<base64>", "mime_type": "image/png", "name": "cat.png"},
  {"type": "document", "path": "report.pdf"},
  {"type": "document", "data": "<base64>", "mime_type": "application/pdf", "name": "report.pdf"}
]
```

- `type: "text"` — uses `text`
- `type: "text"` with `is_attachment=true` — wraps UTF-8 file content as the same `<file ...>` attachment text used by CLI `@file`
- `type: "text"` with `path` (and `is_attachment=true`, no `text`) — server reads the workspace file at `path` (resolved under `cwd`) into the same `<file ...>` snapshot; the path is re-checked at send time (inside `cwd`, regular file, UTF-8, not image/PDF)
- `type: "image"` — uses `path` or inline base64 `data`; workspace-relative `path` attachments set `is_attachment=true` so the server confines them to `cwd`
- `type: "document"` — uses `path` or inline base64 `data`; workspace-relative `path` attachments use the same `is_attachment=true` boundary
- `mime_type` is required when `data` is provided
- `path` accepts `image/png`, `image/jpeg`, `image/gif`, `image/webp`
- `path` accepts `application/pdf` for `type: "document"`
- The resolved model must have `supports_image_input=true`
- The resolved model must have `supports_pdf_input=true` for `type: "document"`

Response:

```json
{
  "run": { "id": "...", "session_id": "...", "kind": "chat", "status": "running", "last_seq": 0 },
  "session": { "id": "...", "title": "...", ... }
}
```

`run.kind` is `"chat"` for `/api/chat` runs and `"compact"` for `/api/sessions/{id}/compact` runs.

Error responses:

- `422` — invalid request shape, such as missing `message`/`input`, both `message` and `input`, or invalid inline media fields; body is FastAPI validation detail (`{"detail": [...]}`)
- `400` — unsupported `reasoning_effort`; body explains whether the provider, model, or value is unsupported
- `400` — invalid `rewind_to`; body is `{"detail": "..."}`
- `400` — missing or invalid `cwd`; body is `{"detail": "Working directory does not exist: ..."}`
- `409` — session already has a running task; body is `{"detail": {"message": "...", "run": {...}}}`
- `500` — provider resolution errors currently bubble up as internal server error in this route

### `GET /api/runs/{run_id}/stream?after=0`

Stream events for a run as SSE (`text/event-stream`).

- `after` — resume from a sequence number (for reconnects); must be `>= 0`
- Each event is a JSON-encoded `StreamEvent` as an SSE `data:` line
- Stream ends with `data: [DONE]`
- All events carry a monotonically increasing `seq` integer

### `POST /api/runs/{run_id}/cancel`

Cancel a running agent run. Pending permission waits resolve as deny. Returns `{status: "ok", run: {...}}` after the run task finishes.

### `POST /api/runs/{run_id}/decide`

Resolve a pending tool permission request. The agent's `before_tool` hook blocks until this is called or the run is cancelled.

Request body (`DecideRequest`, `cli/src/mycode_cli/server/schemas.py`):

```json
{
  "request_id": "...",
  "decision": "allow"
}
```

- `decision` — `"allow"` or `"deny"`; `deny` cancels the active run.
- `request_id` — from the matching `permission_request` SSE event.

Returns `{status: "ok"}` on success, `404` if the run or `request_id` is unknown.

### `POST /api/sessions/{session_id}/compact`

Start a compact run: ask the provider for a summary of the session and append one `compact` marker. No user or assistant turn is created. The run streams over the normal `GET /api/runs/{run_id}/stream` endpoint; success emits a single `compact` event after the marker is persisted.

Request body (`CompactRequest`, `cli/src/mycode_cli/server/schemas.py`):

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6"
}
```

Both fields are optional (config defaults apply). The working directory comes from the session metadata; the summary request carries no tools and no reasoning effort.

Response (`CompactResponse`):

```json
{
  "run": { "id": "...", "session_id": "...", "kind": "compact", "status": "running", "last_seq": 0 }
}
```

Error responses:

- `404` — session not found
- `400` — `{"detail": "nothing to compact"}` when no new user/assistant message follows the latest compact marker
- `409` — session already has a running task; body is `{"detail": {"message": "...", "run": {...}}}`

Cancellation (`POST /api/runs/{run_id}/cancel`) and summary failure write no marker; failures surface as one `error` event and `status: "failed"`.

### `GET /api/config?cwd=...`

Returns current provider configuration for the web UI.

Response:

```json
{
  "providers": {
    "<provider_name>": {
      "name": "...",
      "provider": "anthropic",
      "type": "anthropic",
      "models": ["claude-sonnet-4-6"],
      "base_url": "",
      "has_api_key": true,
      "supports_reasoning_effort": true,
      "reasoning_efforts": {"claude-sonnet-4-6": ["low", "medium", "high"]},
      "supports_image_input": true,
      "image_input_models": ["claude-sonnet-4-6"],
      "supports_pdf_input": true,
      "pdf_input_models": ["claude-sonnet-4-6"]
    }
  },
  "default": { "provider": "<provider_name>", "model": "claude-sonnet-4-6" },
  "cwd": "...",
  "cwd_exists": true,
  "project": "...",
  "config_paths": [...],
  "skills": [{"name": "fastapi", "description": "..."}],
  "setup_error": null
}
```

`reasoning_efforts` maps each model to its available effort values; an empty list means the model has no effort selector. `skills` lists the name and description used by slash completion. Skill paths and contents stay on the server. `image_input_models` lists models with image input. `pdf_input_models` lists models with PDF input. A provider setup error returns status `200`, an empty `providers` object, empty `default` fields, and `setup_error: {"message": "..."}`. A ready setup returns `setup_error: null`.

## Settings

Read and write the **global** config file (`~/.mycode/config.json`). Project-level
`.mycode/config.json` files are not modified by these endpoints; they continue to
override the global file at runtime.

### `GET /api/settings`

Returns the global config plus options for the editor UI.

```json
{
  "path": "/Users/.../.mycode/config.json",
  "exists": true,
  "config": {
    "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    "permission": {"level": "safe", "mode": "ask"},
    "providers": {
      "anthropic": {
        "type": "anthropic",
        "models": ["claude-sonnet-4-6"],
        "api_key": null,
        "api_key_saved": true,
        "base_url": ""
      }
    }
  },
  "options": {
    "provider_types": ["anthropic", "openai", "..."],
    "permission_levels": ["readonly", "safe", "standard", "yolo"],
    "permission_modes": ["ask", "deny"]
  },
  "env": {"ANTHROPIC_API_KEY": true, "OPENAI_API_KEY": false},
  "provider_type_env_vars": {"anthropic": ["ANTHROPIC_API_KEY"]},
  "provider_type_default_models": {"anthropic": ["claude-sonnet-4-6"]}
}
```

- `config.providers.<name>.api_key` is `"${VAR}"` for env references and `null` for both literal secrets and unset values
- `config.providers.<name>.api_key_saved` is `true` only when a literal secret is stored on disk; the secret value itself is never echoed
- `env` reports whether each referenced env var is currently set (built-in env names per provider type plus any `${VAR}` referenced in the config)
- `provider_type_env_vars` — provider type → API key env var names
- `provider_type_default_models` — provider type → default model ids
- `models` is normalised to a list of model ids; per-model metadata overrides come back under `model_overrides` if present

### `PUT /api/settings`

Replace the global config file. Validates input and writes atomically.

```json
{
  "config": {
    "default": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    "permission": {"level": "safe", "mode": "ask"},
    "providers": {
      "anthropic": {
        "type": "anthropic",
        "models": ["claude-sonnet-4-6"],
        "api_key": "sk-...",
        "base_url": ""
      }
    }
  }
}
```

Per-provider `api_key` is three-state:

- `null` (or omitted) — keep the existing value on disk; required when the UI never sees the literal secret
- `""` — clear the field; runtime falls back to the provider type's env discovery
- non-empty string — write verbatim. `${VAR}` syntax is preserved; anything else is stored as a literal secret

Returns the same shape as `GET /api/settings` reflecting the freshly-saved file. Returns `400` with `{"detail": "..."}` for unsupported provider types, out-of-range compact threshold, etc.

## Sessions

All session endpoints are in `cli/src/mycode_cli/server/routers/sessions.py`.

### `GET /api/sessions?cwd=...`

List sessions. Optional `cwd` filters by workspace. Each session includes `is_running` boolean.

Response: `{sessions: [...]}`

### `POST /api/sessions`

Create a new session.

Request body (`SessionCreateRequest`):

```json
{
  "cwd": null
}
```

`cwd` defaults to the server's current working directory. A new uuid-hex `session_id` is allocated.

Response: `{session: {...}, messages: []}`

Missing directories return `400` with `{"detail": "Working directory does not exist: ..."}`.

### `GET /api/sessions/{id}`

Load session with full message history. If the session has an active run, overlays in-memory state:

```json
{
  "session": {...},
  "messages": [...],
  "session_cost": 0.42,
  "active_run": {...} | null,
  "pending_events": [...]
}
```

`pending_events` contains the active run's buffered SSE events. The web UI reapplies them, then reconnects with `after=<last seq>`.

`session_cost` sums persisted `meta.cost.total` values from the raw JSONL timeline, including tool loops, compaction, and rewound turns. Records without cost are skipped; the total is `null` only when no cost is known. During an active run, the value from `usage` SSE events takes precedence.

Assistant and compact messages return their persisted per-request `meta.usage` and `meta.cost` unchanged.

`active_run.kind` distinguishes chat and compact runs. While a compact run is active, `messages` is the pre-run history with no optimistic turn appended; the web UI uses `kind` to restore its `Compacting…` state after a refresh.

### `DELETE /api/sessions/{id}`

Delete session. Returns `409` if session has a running task.

### `POST /api/sessions/{id}/clear`

Clear message history (keeps meta). Returns `409` if session has a running task.

## Workspaces

All workspace endpoints are in `cli/src/mycode_cli/server/routers/workspaces.py`.

### `GET /api/workspaces/roots`

List allowed workspace roots. Roots are read from `MYCODE_WORKSPACE_ROOTS` or `WORKSPACE_ROOTS` env vars (comma-separated paths). Defaults to `$HOME` and `/`.

Response: `{roots: [...]}`

### `GET /api/workspaces/browse?root=...&path=...`

Browse directories within a root. Returns subdirs only, no dotfiles.

Response:

```json
{
  "root": "/Users/example",
  "path": "projects",
  "current": "/Users/example/projects",
  "entries": [{"name": "mycode", "path": "projects/mycode"}],
  "error": ""
}
```

### `GET /api/workspaces/cwd`

Returns current working directory of the server process.

Response: `{cwd: "...", exists: true}`

### `GET /api/workspaces/files?cwd=...&dir=...&prefix=...`

List files and directories under `cwd/dir` for `@` attachment completion. Directories first, then files, both sorted case-insensitively by name. Dotfiles are included. Entries are filtered by `prefix` on the server, then capped at 100 with a `truncated` flag. `kind` is a coarse attachment classification (`directory`, `image`, `document`, `text`) by magic bytes / extension.

Response:

```json
{
  "entries": [
    {"name": "components", "path": "src/components/", "kind": "directory"},
    {"name": "config.ts", "path": "src/config.ts", "kind": "text"}
  ],
  "truncated": false,
  "error": ""
}
```

`error` is non-empty (and `entries` empty) when `cwd` does not exist, `dir` resolves outside `cwd`, or the target is not a directory.

## SSE Contract

`GET /api/runs/{run_id}/stream` produces the following event types. The `StreamEvent` schema is in `cli/src/mycode_cli/server/schemas.py`.

**Do not change event names or payload shapes without updating server, CLI, and web UI.**

| event                 | payload fields                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------ |
| `reasoning`           | `delta: str`                                                                                                 |
| `reasoning_done`      | `duration_ms: int`                                                                                           |
| `text`                | `delta: str`                                                                                                 |
| `tool_start`          | `tool_call: {id, name, input}`                                                                               |
| `tool_output`         | `tool_use_id: str`, `output: str`                                                                            |
| `tool_done`           | `tool_use_id: str`, `output: str`, `is_error: bool`, `metadata?`, `content?`                                 |
| `compact`             | _empty payload_                                                                                              |
| `error`               | `message: str`                                                                                               |
| `permission_request`  | `request_id: str`, `tool_use_id: str`, `tool_name: str`, `preview: str`                                      |
| `permission_resolved` | `request_id: str`, `decision: "allow" \| "deny"`                                                             |
| `usage`               | `context_tokens?`, `context_window?`, `model?`, `turn_usage?`, `turn_cost?`, `session_cost?`                 |

`tool_output` is ordered, append-only display text. Clients do not insert separators between events. Under buffer pressure, `[live output omitted]` replaces one continuous middle segment. `tool_done.output` is the authoritative final result. Once a tool's `tool_done` is buffered, the server may drop that tool's earlier `tool_output` events — a consumer that has not read them yet skips straight to the `tool_done`.

`permission_request` and `permission_resolved` bracket a wait inside the agent's `before_tool` hook. Clients respond via `POST /api/runs/{run_id}/decide`; `permission_resolved` lets reconnecting or second-tab clients dismiss the prompt.

The server adds `model`, `context_window`, and `session_cost` to the SDK usage event described in docs/sdk.md. `context_tokens` is the latest normal request's context usage; `turn_usage` and `turn_cost` are cumulative snapshots for the turn. `session_cost` sums the known pre-run session and current turn totals. All costs are USD. SSE omits `None` fields; absence means the current snapshot is unavailable and clients must clear any previous value.

Every event also carries `seq: int` for reconnect support. The web UI uses `after` to resume after a sequence number. The reconnect cache is bounded by event count and tool-output bytes; if older events were evicted, the first returned `seq` is greater than `after + 1`. The server does not synthesize or rewrite events to represent that gap.

## Run Manager

`cli/src/mycode_cli/server/run_manager.py` manages concurrent runs:

- One active run per session (enforced by `ActiveRunError` on conflict, regardless of kind)
- Two run kinds share the lifecycle: `start_run()` iterates `agent.achat(user_message)`; `start_compact()` awaits `agent.acompact()` and emits one `compact` event after the marker is persisted
- Compact runs carry no `user_message`; snapshots return `base_messages` unchanged
- `RunState` tracks a bounded reconnect event buffer and condition variable for streaming
- Explicit permission `deny` marks the run as cancelled and calls `agent.cancel()`
- `cancel_run()` waits for the agent task to finish before returning the final run info
- Finished runs pruned after 300 seconds (`FINISHED_RUN_TTL_SECONDS`)
- `snapshot_session()` returns reconnect data (base messages + buffered events) for active runs
