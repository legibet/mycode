# Sessions

Source: `mycode/core/session.py`

## Storage Layout

```
~/.mycode/sessions/<session_id>/
  meta.json        # session metadata
  messages.jsonl   # one JSON record per line (append-only)
  tool-output/     # large bash outputs spilled to disk
```

Sessions directory resolved by `resolve_sessions_dir()` → `$MYCODE_HOME/sessions/` (default `~/.mycode/sessions/`).

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
- `provider` / `model` / `api_base` — fixed at session creation; request-time overrides are runtime-only
- `message_format_version` — written as `5` when missing; no version check on load

## messages.jsonl Record Types

Each line is a JSON object. The `role` field acts as a discriminator.

### Regular message

Standard `user` or `assistant` message in the internal block format (see AGENTS.md).

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

Marks a context compaction point. The agent loop writes a compact event when token usage ≥ `compact_threshold` × context window. See "Context Compaction" below.

### Rewind event

```json
{"role": "rewind", "meta": {"rewind_to": 5, "created_at": "..."}}
```

Marks an undo point. See "Rewind" below.

## Load Order

When a session is loaded (`SessionStore.load_session`):

1. Read all JSONL lines into a raw list
2. `apply_compact()` — find the last `role: "compact"` record, replace everything before it with a synthetic user summary + assistant ack, keep messages after
3. `apply_rewind()` — scan sequentially; when a rewind record is found, truncate the accumulated list to `meta.rewind_to` and continue loading subsequent lines
4. `_repair_interrupted_tool_loop()` — if the latest assistant tool loop has unmatched `tool_use` blocks (no corresponding `tool_result` user message), append one synthetic error result message

## Context Compaction

Triggered in `Agent._compact_if_needed()` after a successful turn completes:

1. Check `should_compact()` — true when last assistant message's `usage.input_tokens` ≥ `context_window × compact_threshold`
2. Ask the same provider for a summary (no tools, just text, max 8192 tokens)
3. Build a compact event with the summary text and `compacted_count`
4. Persist the compact event (append-only — original messages stay in JSONL)
5. Apply `apply_compact()` in memory to rebuild the message list
6. Emit SSE `compact` event to the web UI

`should_compact()` checks multiple usage field names: `input_tokens`, `prompt_tokens`, `prompt_token_count`.

## Rewind

Triggered by `POST /api/chat` with `rewind_to` parameter:

1. Server validates the target message is a real user message
2. Optimistically truncates messages in memory
3. On first persist, appends a rewind event to JSONL
4. On load, `apply_rewind()` processes rewind markers inline

## tool-output/ Spill

Bash output exceeding 5MB in memory (`_BASH_MAX_IN_MEMORY_BYTES`) is written to `tool-output/bash-<tool_call_id>.log`. The tool result keeps the last 2000 lines in memory. When output is truncated, the result text includes the saved log path and instructions to read it with offset/limit.

## Current Format Version

`MESSAGE_FORMAT_VERSION = 5`

Stored in `meta.json`. The field is written when missing but not validated on load — newer versions are accepted silently.

## Session Store API

`SessionStore` (in `mycode/core/session.py`) provides:

- `create_session(title, *, session_id, provider, model, cwd, api_base)` → create on disk
- `list_sessions(*, cwd)` → list by workspace, sorted by `updated_at` desc
- `load_session(session_id)` → load with full replay pipeline
- `delete_session(session_id)` → recursive directory delete
- `clear_session(session_id)` → truncate messages.jsonl, keep meta
- `append_message(session_id, message, *, provider, model, cwd, api_base)` → append one line; auto-creates session on first message
- `append_rewind(session_id, rewind_to)` → append rewind marker

All file I/O is offloaded to `asyncio.to_thread()` to avoid blocking the event loop.
