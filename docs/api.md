# Server API

Base prefix: `/api`. All endpoints are defined in `mycode/server/routers/`.

## Chat

### `POST /api/chat`

Start an agent run. Returns JSON immediately while the run streams asynchronously.

Request body (`ChatRequest`, `mycode/server/schemas.py`):

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
  {"type": "image", "path": "cat.png"},
  {"type": "image", "data": "<base64>", "mime_type": "image/png", "name": "cat.png"}
]
```

- `type: "text"` — uses `text`
- `type: "image"` — uses `path` or inline base64 `data`
- `mime_type` is required when `data` is provided
- `path` accepts `image/png`, `image/jpeg`, `image/gif`, `image/webp`
- The resolved model must have `supports_image_input=true`

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

### `GET /api/config?cwd=...`

Returns current provider configuration for the frontend.

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
      "image_input_models": ["claude-sonnet-4-6"]
    }
  },
  "default": { "provider": "<provider_name>", "model": "claude-sonnet-4-6" },
  "default_reasoning_effort": "auto",
  "reasoning_effort_options": ["auto", "none", "low", "medium", "high", "xhigh"],
  "cwd": "...",
  "workspace_root": "...",
  "config_paths": [...]
}
```

`reasoning_models` is returned only when `supports_reasoning_effort` is true. `image_input_models` lists models with `supports_image_input=true`.

## Sessions

All session endpoints are in `mycode/server/routers/sessions.py`.

### `GET /api/sessions?cwd=...`

List sessions. Optional `cwd` filters by workspace. Each session includes `is_running` boolean.

Response: `{sessions: [...]}`

### `POST /api/sessions`

Create a new session.

Request body (`SessionCreateRequest`):

```json
{
  "title": null,
  "provider": null,
  "model": null,
  "cwd": null,
  "api_base": null
}
```

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

`pending_events` contains the active run's buffered SSE events. The frontend reapplies them, then reconnects with `after=<last seq>`.

### `DELETE /api/sessions/{id}`

Delete session. Returns `409` if session has a running task.

### `POST /api/sessions/{id}/clear`

Clear message history (keeps meta). Returns `409` if session has a running task.

## Workspaces

All workspace endpoints are in `mycode/server/routers/workspaces.py`.

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

`GET /api/runs/{run_id}/stream` produces the following event types. The `StreamEvent` schema is in `mycode/server/schemas.py`.

**Do not change event names or payload shapes without updating server, CLI, and frontend.**

| event         | payload fields                                                               |
| ------------- | ---------------------------------------------------------------------------- |
| `reasoning`   | `delta: str`                                                                 |
| `text`        | `delta: str`                                                                 |
| `tool_start`  | `tool_call: {id, name, input}`                                               |
| `tool_output` | `tool_use_id: str`, `output: str`                                            |
| `tool_done`   | `tool_use_id: str`, `model_text: str`, `display_text: str`, `is_error: bool` |
| `compact`     | `message: str`                                                               |
| `error`       | `message: str`                                                               |

Every event also carries `seq: int` for reconnect support. The frontend uses `after` parameter to resume from a specific seq number.

## Run Manager

`mycode/server/run_manager.py` manages concurrent runs:

- One active run per session (enforced by `ActiveRunError` on conflict)
- `RunState` tracks events, condition variable for streaming, and cleanup
- Finished runs pruned after 300 seconds (`FINISHED_RUN_TTL_SECONDS`)
- `snapshot_session()` returns reconnect data (base messages + buffered events) for active runs
