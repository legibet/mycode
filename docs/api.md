# Server API

Base prefix: `/api`

## Endpoints

### Chat

**`POST /api/chat`** — Start an agent run. Returns a streaming SSE response.

Request body (`ChatRequest`):

```json
{
  "message": "...",
  "session_id": "...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "cwd": "/path/to/workspace",
  "reasoning_effort": "medium",
  "max_turns": null
}
```

All fields except `message` are optional. `reasoning_effort` overrides config for this request only.

**`GET /api/config`** — Returns current provider and reasoning configuration for the frontend.

Response:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "supports_reasoning_effort": true,
  "reasoning_models": ["claude-sonnet-4-6", "claude-opus-4-6"],
  "reasoning_effort": "auto",
  "default_reasoning_effort": "auto",
  "reasoning_effort_options": ["auto", "none", "low", "medium", "high", "xhigh"]
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

**`GET /api/workspaces/browse?root=...&path=...`** — Browse directories within a root. Returns entries (subdirs only, no dotfiles).

**`GET /api/workspaces/cwd`** — Returns current working directory of the server process.

## SSE Contract

`POST /api/chat` streams `text/event-stream` with the following event types.

**Do not change event names or payload shapes without updating server, CLI, and frontend.**

| event         | payload fields                                      |
| ------------- | --------------------------------------------------- |
| `reasoning`   | `delta: str`                                        |
| `text`        | `delta: str`                                        |
| `tool_start`  | `tool_call: {id, name, input}`                      |
| `tool_output` | `tool_use_id: str`, `output: str`                   |
| `tool_done`   | `tool_use_id: str`, `result: str`, `is_error: bool` |
| `error`       | `message: str`                                      |

Each event is a JSON-encoded `StreamEvent` object emitted as an SSE `data:` line.

The stream closes when the agent loop completes or an unrecoverable error occurs.
