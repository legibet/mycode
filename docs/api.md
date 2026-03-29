# Server API

Base prefix: `/api`

## Endpoints

### Chat

**`POST /api/chat`** — Start an agent run. Returns a JSON response immediately.

Request body (`ChatRequest`):

```json
{
  "message": "...",
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

All fields except `message` are optional. `reasoning_effort` overrides config for this request only. `rewind_to` is a visible message index; if set, the conversation is rewound to that point before the new message is sent.

Response:

```json
{
  "run": { "id": "...", "session_id": "...", "status": "running" },
  "session": { "id": "...", "title": "...", ... }
}
```

**`GET /api/runs/{run_id}/stream?after=0`** — Stream events for a run as SSE. `after` resumes from a sequence number (for reconnects).

**`POST /api/runs/{run_id}/cancel`** — Cancel a running agent run.

**`GET /api/config?cwd=...`** — Returns current provider configuration for the frontend.

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
      "reasoning_effort": "auto"
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

### Sessions

**`GET /api/sessions`** — List sessions. Query params: `cwd` (filter by workspace).

**`POST /api/sessions`** — Create a new session.

**`GET /api/sessions/{id}`** — Load session with full message history.

**`DELETE /api/sessions/{id}`** — Delete session.

**`POST /api/sessions/{id}/clear`** — Clear message history (keeps meta).

### Workspaces

**`GET /api/workspaces/roots`** — List allowed workspace roots.

Roots are read from `MYCODE_WORKSPACE_ROOTS` or `WORKSPACE_ROOTS` env vars (comma-separated paths). Defaults to `$HOME` and `/`.

**`GET /api/workspaces/browse?root=...&path=...`** — Browse directories within a root. Returns subdirs only, no dotfiles.

**`GET /api/workspaces/cwd`** — Returns current working directory of the server process.

## SSE Contract

`GET /api/runs/{run_id}/stream` streams `text/event-stream` with the following event types.

**Do not change event names or payload shapes without updating server, CLI, and frontend.**

| event         | payload fields                                                               |
| ------------- | ---------------------------------------------------------------------------- |
| `reasoning`   | `delta: str`                                                                 |
| `text`        | `delta: str`                                                                 |
| `tool_start`  | `tool_call: {id, name, input}`                                               |
| `tool_output` | `tool_use_id: str`, `output: str`                                            |
| `tool_done`   | `tool_use_id: str`, `model_text: str`, `display_text: str`, `is_error: bool` |
| `compact`     | `message: str`, `compacted_count: int`                                       |
| `error`       | `message: str`                                                               |

Each event is a JSON-encoded `StreamEvent` object emitted as an SSE `data:` line. The stream ends with `data: [DONE]`.

All events carry a monotonically increasing `seq` integer for reconnect support.
