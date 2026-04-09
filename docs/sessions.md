# Sessions

Source: `mycode-go/internal/session/store.go`

## Storage Layout

```text
~/.mycode/sessions/<session_id>/
  meta.json        # session metadata
  messages.jsonl   # one JSON record per line (append-only)
  tool-output/     # large bash outputs spilled to disk
```

Sessions directory is resolved by `ResolveSessionsDir()` → `$MYCODE_HOME/sessions/` (default `~/.mycode/sessions/`).

## meta.json

```json
{
  "id": "...",
  "title": "...",
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "cwd": "/path/to/workspace",
  "api_base": null,
  "message_format_version": 5,
  "created_at": "...",
  "updated_at": "..."
}
```

- `title` — auto-set from the first user message text (first 48 chars)
- `provider`, `model`, `api_base` — fixed at session creation; request-time overrides are runtime-only
- `message_format_version` — written as `5` when missing; no version check on load

## messages.jsonl Record Types

Each line is a JSON object. The `role` field acts as a discriminator.

### Regular message

Standard `user` or `assistant` message in the internal block format.

```json
{"role": "user", "content": [{"type": "text", "text": "..."}], "meta": {...}}
{"role": "user", "content": [{"type": "text", "text": "..."}, {"type": "image", "data": "...", "mime_type": "image/png"}], "meta": {...}}
{"role": "assistant", "content": [{"type": "thinking", "text": "..."}, {"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}], "meta": {"provider": "...", "model": "...", "stop_reason": "...", "usage": {...}}}
```

`tool_result.content` may store `text` and `image` blocks.

### Compact event

```json
{"role": "compact", "content": [{"type": "text", "text": "<summary>"}], "meta": {"provider": "...", "model": "...", "compacted_count": 12}}
```

Marks a context compaction point. The agent loop writes a compact event when token usage ≥ `compact_threshold × context window`. See "Context Compaction" below.

### Rewind event

```json
{"role": "rewind", "meta": {"rewind_to": 5, "created_at": "..."}}
```

Marks an undo point. See "Rewind" below.

## Load Order

When a session is loaded (`Store.LoadSession`):

1. Read all JSONL lines into a raw list
2. `ApplyCompact()` — find the last `role: "compact"` record, replace everything before it with a synthetic user summary + assistant ack, keep messages after it
3. `ApplyRewind()` — scan sequentially; when a rewind record is found, truncate the accumulated list to `meta.rewind_to` and continue loading subsequent lines
4. `repairInterruptedToolLoop()` — if the latest assistant tool loop has unmatched `tool_use` blocks (no corresponding `tool_result` user message), append one synthetic error result message

## Context Compaction

Triggered in the agent loop after a successful turn completes:

1. Check `ShouldCompact()` — true when the last assistant message's `usage.input_tokens` ≥ `context_window × compact_threshold`
2. Ask the same provider for a summary (no tools, just text, max 8192 tokens)
3. Build a compact event with the summary text and `compacted_count`
4. Persist the compact event (append-only — original messages stay in JSONL)
5. Apply `ApplyCompact()` in memory to rebuild the visible message list
6. Emit SSE `compact` event to the web UI

`ShouldCompact()` checks multiple usage field names: `input_tokens`, `prompt_tokens`, `prompt_token_count`.

## Rewind

Triggered by `POST /api/chat` with `rewind_to`:

1. Server validates the target message is a real user message
2. Optimistically truncates visible messages in memory
3. On first persist, appends a rewind event to JSONL
4. On load, `ApplyRewind()` processes rewind markers inline

## tool-output Spill

Bash output exceeding 5MB in memory (`BashMaxInMemoryBytes`) is written to `tool-output/bash-<tool_call_id>.log`. The tool result keeps the last 2000 lines in memory. When output is truncated, the result text includes the saved log path and instructions to read it with offset or limit.

## Current Format Version

`MessageFormatVersion = 5`

Stored in `meta.json`. The field is written when missing but not validated on load.

## Session Store API

`Store` (in `mycode-go/internal/session/store.go`) provides:

- `CreateSession(title, sessionID, provider, model, cwd, apiBase)` → create on disk
- `ListSessions(cwd)` → list by workspace, sorted by `updated_at` descending
- `LoadSession(sessionID)` → load with full replay pipeline
- `DeleteSession(sessionID)` → recursive directory delete
- `ClearSession(sessionID)` → truncate `messages.jsonl`, keep meta
- `AppendMessage(sessionID, message, provider, model, cwd, apiBase)` → append one line; auto-creates a session on first message
- `AppendRewind(sessionID, rewindTo)` → append rewind marker when the session already exists
