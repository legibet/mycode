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
- `get_or_create()` preserves existing meta when resuming a session

## messages.jsonl Record Types

Each line is a JSON object with a `_type` discriminator (or none for regular messages).

### Regular message

```json
{"role": "user"|"assistant", "content": [...blocks...], "meta": {...}}
```

No `_type` field. See AGENTS.md for the full block format.

### Compact event

```json
{"_type": "compact", "summary": "...", "kept_messages": [...]}
```

Marks a context compaction point. On load, messages before this event are replaced with a synthetic summary message + `kept_messages`. Agent loop triggers compaction when token usage ≥ `compact_threshold` (default 0.8 × context window).

### Rewind event

```json
{"_type": "rewind", "target_index": 5}
```

Marks an undo point. On load, messages after `target_index` are discarded (in memory only — the JSONL file is not truncated).

## Load Order

When a session is loaded from disk:

1. Scan all lines; identify compact and rewind event positions
2. Apply the most recent compact event (replace prefix with summary)
3. Apply any rewind events (truncate at target index)
4. Scan for interrupted tool calls (tool_use with no matching tool_result) → insert synthetic error blocks

## tool-output/ Spill

Bash output exceeding the inline size limit is written to `tool-output/<tool_use_id>.txt`. The tool result stored in JSONL contains a reference path instead of the full output.

## Current Format Version

`MESSAGE_FORMAT_VERSION = 5`

The version is stored in `meta.json`. The runtime only reads sessions at the current version; older formats are not auto-migrated.
