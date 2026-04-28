# Server API

Base prefix: `/api`. All endpoints are defined in `cli/src/mycode_cli/server/routers/`.

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
- `reasoning_effort` — overrides config for this request only; `null`/`"auto"` means use config default
- `rewind_to` — visible message index to rewind to before sending the new message; target must be a real user message

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
- `type: "image"` — uses `path` or inline base64 `data`
- `type: "document"` — uses `path` or inline base64 `data`
- `mime_type` is required when `data` is provided
- `path` accepts `image/png`, `image/jpeg`, `image/gif`, `image/webp`
- `path` accepts `application/pdf` for `type: "document"`
- The resolved model must have `supports_image_input=true`
- The resolved model must have `supports_pdf_input=true` for `type: "document"`

Response:

```json
{
  "run": { "id": "...", "session_id": "...", "status": "running", "last_seq": 0 },
  "session": { "id": "...", "title": "...", ... }
}
```

Error responses:

- `400` — invalid `rewind_to`; body is `{"detail": "..."}`
- `409` — session already has a running task; body is `{"detail": {"message": "...", "run": {...}}}`
- `500` — provider resolution errors currently bubble up as internal server error in this route

### `GET /api/runs/{run_id}/stream?after=0`

Stream events for a run as SSE (`text/event-stream`).

- `after` — resume from a sequence number (for reconnects)
- Each event is a JSON-encoded `StreamEvent` as an SSE `data:` line
- Stream ends with `data: [DONE]`
- All events carry a monotonically increasing `seq` integer

### `POST /api/runs/{run_id}/cancel`

Cancel a running agent run. Returns `{status: "ok", run: {...}}`.

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
      "reasoning_models": ["claude-sonnet-4-6"],
      "reasoning_effort": "auto",
      "supports_image_input": true,
      "image_input_models": ["claude-sonnet-4-6"],
      "supports_pdf_input": true,
      "pdf_input_models": ["claude-sonnet-4-6"]
    }
  },
  "default": { "provider": "<provider_name>", "model": "claude-sonnet-4-6" },
  "default_reasoning_effort": "auto",
  "reasoning_effort_options": ["auto", "none", "low", "medium", "high", "xhigh"],
  "cwd": "...",
  "project": "...",
  "config_paths": [...]
}
```

`reasoning_models` is returned only when `supports_reasoning_effort` is true. `image_input_models` lists models with `supports_image_input=true`. `pdf_input_models` lists models with `supports_pdf_input=true`.

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
        "base_url": "",
        "reasoning_effort": null
      }
    }
  },
  "options": {
    "provider_types": ["anthropic", "openai", "..."],
    "permission_levels": ["readonly", "safe", "standard", "yolo"],
    "permission_modes": ["ask", "deny"],
    "reasoning_efforts": ["auto", "none", "low", "medium", "high", "xhigh"]
  },
  "env": {"ANTHROPIC_API_KEY": true, "OPENAI_API_KEY": false},
  "provider_type_env_vars": {"anthropic": ["ANTHROPIC_API_KEY"]}
}
```

- `config.providers.<name>.api_key` is `"${VAR}"` for env references and `null` for both literal secrets and unset values
- `config.providers.<name>.api_key_saved` is `true` only when a literal secret is stored on disk; the secret value itself is never echoed
- `env` reports whether each referenced env var is currently set (built-in env names per provider type plus any `${VAR}` referenced in the config)
- `models` is normalised to a list of model ids; per-model metadata overrides come back under `model_overrides` if present

### `PUT /api/settings`

Replace the global config file. Validates input via `validate_global_config` and writes atomically.

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
        "base_url": "",
        "reasoning_effort": "auto"
      }
    }
  }
}
```

Per-provider `api_key` is three-state:

- `null` (or omitted) — keep the existing value on disk; required when the UI never sees the literal secret
- `""` — clear the field; runtime falls back to the provider type's env discovery
- non-empty string — write verbatim. `${VAR}` syntax is preserved; anything else is stored as a literal secret

Returns the same shape as `GET /api/settings` reflecting the freshly-saved file. Returns `400` with `{"detail": "..."}` for unsupported provider types, invalid reasoning effort, out-of-range compact threshold, etc.

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

### `GET /api/sessions/{id}`

Load session with full message history. If the session has an active run, overlays in-memory state:

```json
{
  "session": {...},
  "messages": [...],
  "active_run": {...} | null,
  "pending_events": [...]
}
```

`pending_events` contains the active run's buffered SSE events. The web UI reapplies them, then reconnects with `after=<last seq>`.

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

## SSE Contract

`GET /api/runs/{run_id}/stream` produces the following event types. The `StreamEvent` schema is in `cli/src/mycode_cli/server/schemas.py`.

**Do not change event names or payload shapes without updating server, CLI, and web UI.**

| event                 | payload fields                                                               |
| --------------------- | ---------------------------------------------------------------------------- |
| `reasoning`           | `delta: str`                                                                 |
| `reasoning_done`      | `duration_ms: int`                                                           |
| `text`                | `delta: str`                                                                 |
| `tool_start`          | `tool_call: {id, name, input}`                                               |
| `tool_output`         | `tool_use_id: str`, `output: str`                                            |
| `tool_done`           | `tool_use_id: str`, `output: str`, `is_error: bool`, `metadata?`, `content?` |
| `compact`             | `message: str`                                                               |
| `error`               | `message: str`                                                               |
| `permission_request`  | `request_id: str`, `tool_use_id: str`, `tool_name: str`, `preview: str`      |
| `permission_resolved` | `request_id: str`, `decision: "allow" \| "deny"`                             |

`permission_request` and `permission_resolved` bracket a wait inside the agent's `before_tool` hook. Clients respond via `POST /api/runs/{run_id}/decide`; `permission_resolved` lets reconnecting or second-tab clients dismiss the prompt.

Every event also carries `seq: int` for reconnect support. The web UI uses `after` parameter to resume from a specific seq number.

## Run Manager

`cli/src/mycode_cli/server/run_manager.py` manages concurrent runs:

- One active run per session (enforced by `ActiveRunError` on conflict)
- `RunState` tracks events, condition variable for streaming, and cleanup
- Finished runs pruned after 300 seconds (`FINISHED_RUN_TTL_SECONDS`)
- `snapshot_session()` returns reconnect data (base messages + buffered events) for active runs
