# Sessions

## Storage Layout

```
~/.mycode/sessions/<session_id>/
  meta.json        # session metadata
  messages.jsonl   # one JSON record per line (append-only)
  tool-output/     # large bash outputs spilled to disk
```

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

- `title` is auto-set from the first user message text
- `provider` / `model` / `api_base` are fixed at session creation; request-time overrides are runtime-only

## messages.jsonl Record Types

Each line is a JSON object. The `role` field acts as a discriminator for all record types.

### Regular message

Standard `user` or `assistant` message. See AGENTS.md for the full block format.

```json
{"role": "user", "content": [...blocks...], "meta": {...}}
{"role": "assistant", "content": [...blocks...], "meta": {"provider": "...", "model": "...", ...}}
```

### Compact event

```json
{"role": "compact", "content": [{"type": "text", "text": "<summary>"}], "meta": {"provider": "...", "model": "...", "compacted_count": 12}}
```

Marks a context compaction point. On load, `apply_compact()` finds the last `role: "compact"` record, converts it to a synthetic user+assistant summary exchange, and returns that followed by messages after the compact event. The agent loop writes a compact event when token usage ≥ `compact_threshold` × context window.

### Rewind event

```json
{"role": "rewind", "meta": {"rewind_to": 5, "created_at": "..."}}
```

Marks an undo point. `apply_rewind()` scans the raw JSONL sequentially; when it encounters a rewind record, it truncates the accumulated list to `meta.rewind_to` and continues loading subsequent lines.

## Load Order

When a session is loaded:

1. Read all JSONL lines into a raw list
2. `apply_compact()` — replace prefix up to the last compact event with a summary exchange
3. `apply_rewind()` — process rewind markers inline, truncating as encountered
4. `_repair_interrupted_tool_loop()` — insert synthetic error blocks for any unmatched `tool_use` at the end

## tool-output/ Spill

Bash output exceeding the inline size limit is written to `tool-output/bash-<tool_call_id>.log`. The tool result stored in JSONL keeps the usual text fields; when output is spilled or truncated, that text includes the saved log path.

## Current Format Version

`MESSAGE_FORMAT_VERSION = 5`

Stored in `meta.json`. No version check on load — the field is written when missing but not validated.
