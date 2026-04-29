# Sessions

Source: `mycode/src/mycode/session.py`

## Storage Layout

```text
<data_dir>/<session_id>/
  meta.json        # immutable session metadata
  messages.jsonl   # one JSON record per line (append-only)
  tool-output/     # bash spill files (lazy; created on first spill)
```

`data_dir` is supplied by the caller. The SDK never picks a default path. The CLI resolves it to `$MYCODE_HOME/sessions/` (default `~/.mycode/sessions/`) via `mycode_cli.config.resolve_sessions_dir()`.

`tool-output/` is the per-session directory Agent passes into the `ToolContext`. It's created lazily on the first bash spill; custom tools can treat it as scratch space.

## meta.json

```json
{
  "cwd": "/path/to/workspace",
  "title": "...",
  "created_at": "...",
  "updated_at": "...",
  "message_format_version": 6
}
```

- `cwd` — workspace path recorded at session creation; used by `list_sessions(cwd=...)` for filtering
- `title` — defaults to `"New chat"`; promoted to the first user message text (truncated to 48 chars) on the first `append_message` carrying readable user text
- `updated_at` — bumped on every `append_message`
- `message_format_version` — written as `6`, not validated on load

Per-turn state (`provider` / `model` / `api_base`) intentionally lives only on each `ConversationMessage.meta`; caching a "current" value at the session level would drift after `/model` switches.

## messages.jsonl Record Types

Each line is a JSON object. The `role` field acts as a discriminator.

### Regular message

Standard `user` or `assistant` message in the internal block format.

```json
{"role": "user", "content": [{"type": "text", "text": "..."}], "meta": {...}}
{"role": "assistant", "content": [{"type": "thinking", "text": "...", "meta": {"duration_ms": 1200}}, {"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}], "meta": {"provider": "...", "model": "...", "stop_reason": "...", "total_tokens": 1456, "context_window": 200000}}
```

`tool_result.content` may store `text` and `image` blocks.

`assistant.meta.total_tokens` is the canonical token count for the call: prompt plus everything the model produced this turn (text, tool calls, reasoning). It also equals the prompt floor of the next API call (history accumulated up to and including this turn), which is what `should_compact` and the consumed-context UI both compare against `context_window`.

`assistant.meta.context_window` is the model's full context window for the call, resolved from the catalog at runtime. Stamped onto the message so clients can render the consumed-context percentage without re-deriving model metadata. Absent when unavailable.

Adapter normalization for `total_tokens`:

| provider       | source                                                  |
| -------------- | ------------------------------------------------------- |
| `anthropic*`   | `input_tokens + cache_* + output_tokens` (no `total`)   |
| `openai`       | `total_tokens`                                          |
| `openai_chat*` | `total_tokens`                                          |
| `google`       | `total_token_count` (includes thoughts and tool prompt) |

### Compact event

```json
{"role": "compact", "content": [{"type": "text", "text": "<summary>"}], "meta": {"provider": "...", "model": "...", "compacted_count": 12}}
```

Marks a context compaction point. Written when token usage ≥ `compact_threshold × context_window`. See "Context Compaction" below.

### Rewind event

```json
{"role": "rewind", "meta": {"rewind_to": 5, "created_at": "..."}}
```

Marks an undo point. See "Rewind" below.

## Load Order

When `SessionStore.load_session` runs:

1. Read all JSONL lines into a raw list
2. `apply_compact()` — find the last `role: "compact"` record, replace everything before it with a synthetic user summary + assistant ack, keep messages after
3. `apply_rewind()` — scan sequentially; when a rewind record is found, truncate the accumulated list to `meta.rewind_to` and continue

`load_session` is a pure reader. Orphan `tool_use` blocks left by an interrupted run are closed by the provider adapter when the messages are replayed, not by the loader.

## Context Compaction

Checked after every completed assistant turn (with or without tools), always at
a full `assistant`/`tool_result` boundary.

1. `should_compact()` — true when the latest assistant message's `total_tokens` ≥ `context_window × compact_threshold` (default `0.8`). Tool outputs appended this turn aren't reflected in that figure until the next API call's usage; the `(1 - threshold)` headroom absorbs them.
2. Ask the same provider for a summary (no tools, text only, max 8192 tokens)
3. Build a compact event with the summary text and `compacted_count`
4. Persist the compact event (append-only — original messages stay in JSONL)
5. Apply `apply_compact()` in memory to rebuild the message list
6. Emit the `compact` event to the caller

The headroom `(1 - compact_threshold) × context_window` is reserved for the
compact call itself: that call sends the full current history as input plus a
summary capped at the agent's max output tokens. Lowering `compact_threshold`
just triggers earlier; raising it is bounded by how much headroom the compact
call needs to fit.

## Rewind

Triggered by `POST /api/chat` with `rewind_to`:

1. Server validates the target is a real user message
2. Server calls `append_rewind(session_id, rewind_to)` — appends a rewind marker to JSONL
3. Agent auto-resumes; `apply_rewind()` produces the truncated visible history

## tool-output/ Spill

Bash output exceeding 5MB in memory (`_BASH_MAX_IN_MEMORY_BYTES`) is written to `<tool_output_dir>/bash-<tool_call_id>.log`. The tool result keeps the last 2000 lines in memory and cites the saved log path.

`tool_output_dir` is always set — Agent defaults it to a session-adjacent directory when persistence is configured, or a tempdir-scoped equivalent otherwise. The directory itself is created lazily on first spill.

## Session Store API

`SessionStore` (in `mycode/src/mycode/session.py`):

- `SessionStore(data_dir: Path)` — required; no default
- `session_exists(session_id)` — check by `meta.json` presence
- `create_session(session_id, *, cwd)` — write `meta.json` and touch `messages.jsonl`
- `list_sessions(*, cwd=None)` — filter by workspace, sorted by `updated_at` desc; derived `title` / `updated_at` included per entry
- `load_session(session_id)` — load with full replay pipeline (returns `None` when absent)
- `delete_session(session_id)` — recursive directory delete
- `clear_session(session_id)` — truncate `messages.jsonl`, reset `title` to the default and bump `updated_at`
- `append_message(session_id, message)` — append one line; refresh meta's `updated_at` and promote `title` on the first user message
- `append_rewind(session_id, rewind_to)` — append a rewind marker

All file I/O is offloaded to `asyncio.to_thread()`.
