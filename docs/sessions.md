# Sessions

Source: `mycode/src/mycode/session.py`

## Storage Layout

```text
<data_dir>/
  index.json       # session list cache
  <session_id>/
    meta.json      # session metadata
    messages.jsonl # one JSON record per line (append-only)
    tool-output/   # bash spill files (lazy; created on first spill)
```

`data_dir` is supplied by the caller. The SDK never picks a default path. The CLI resolves it to `$MYCODE_HOME/sessions/` (default `~/.mycode/sessions/`) via `mycode_cli.config.resolve_sessions_dir()`.

`tool-output/` is the per-session directory Agent passes into the `ToolContext`. It's created lazily on the first bash spill; custom tools can treat it as scratch space.

## meta.json

```json
{
  "cwd": "/path/to/workspace",
  "title": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

- `cwd` — workspace path recorded at session creation; used by `list_sessions(cwd=...)` for filtering
- `title` — defaults to `"New chat"`; promoted to the first user message text (truncated to 48 chars) on the first `append_message` carrying readable user text
- `updated_at` — bumped on every `append_message`

Per-turn state (`provider` / `model` / `api_base`) intentionally lives only on each `ConversationMessage.meta`; caching a "current" value at the session level would drift after `/model` switches.

## index.json

```json
{
  "session-id": {
    "cwd": "/path/to/workspace",
    "title": "...",
    "created_at": "...",
    "updated_at": "..."
  }
}
```

`index.json` is a map from session id to session metadata. `list_sessions()` reads it directly; missing or invalid index data is rebuilt from existing `meta.json` files.

## messages.jsonl Record Types

Each line is a JSON object. The `role` field acts as a discriminator.

### Regular message

Standard `user` or `assistant` message in the internal block format.

```json
{"role": "user", "content": [{"type": "text", "text": "..."}], "meta": {...}}
{"role": "assistant", "content": [{"type": "thinking", "text": "...", "meta": {"duration_ms": 1200}}, {"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}], "meta": {"provider": "...", "model": "...", "stop_reason": "...", "usage": {"total_tokens": 1456, "input_tokens": 1400, "cache_read_tokens": 1200, "cache_write_tokens": 100, "output_tokens": 56, "reasoning_tokens": 20, "reported_cost_usd": 0.0123}, "context_window": 200000}}
```

`assistant.meta.model` records the selected request model, not a provider-returned alias or routed model name.

`tool_result.content` may store `text` and `image` blocks.

Each matching `/<skill-name>` token prepends a text block with `meta.skill_snapshot=true` before the original user text. The snapshot contains the frontmatter-free `SKILL.md` body, source path, and base directory. The session persists it for provider replay. Session titles and TUI/Web history use the original text.

Snapshots stay in the session timeline across compaction, but a snapshot before the last `compact` marker reaches providers only through the summary; its `location` lets the model re-read the skill file.

`assistant.meta.usage` holds canonical facts for one provider request. A missing key means the provider did not report it.

| field                | semantics                                                              |
| -------------------- | ---------------------------------------------------------------------- |
| `total_tokens`       | upstream official total, else `input_tokens + output_tokens`           |
| `input_tokens`       | full effective input, **including** cache reads and writes             |
| `cache_read_tokens`  | subset of `input_tokens`                                               |
| `cache_write_tokens` | subset of `input_tokens`                                               |
| `output_tokens`      | full output, **including** reasoning                                   |
| `reasoning_tokens`   | subset of `output_tokens`                                              |
| `reported_cost_usd`  | upstream-reported charge only; currently OpenRouter                    |

`usage.total_tokens` is the request's context metric. Compaction and context displays compare it with `context_window`.

`assistant.meta.context_window` is the model's context window for the request, resolved from the catalog. It is absent when unavailable.

Cancelled streams may persist partial assistant content without `usage` because final usage never arrived.

Adapter normalization (canonical ← raw; missing fields stay unknown unless noted in docs/providers.md):

| provider       | input                                              | cache read / write                                            | output                                          | reasoning                                    |
| -------------- | -------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------- | -------------------------------------------- |
| `anthropic*`   | `input_tokens + cache_creation_* + cache_read_*`   | `cache_read_input_tokens` / `cache_creation_input_tokens`     | `output_tokens`                                 | `output_tokens_details.thinking_tokens`      |
| `openai`       | `input_tokens`                                     | `input_tokens_details.cached_tokens` / `.cache_write_tokens`  | `output_tokens`                                 | `output_tokens_details.reasoning_tokens`     |
| `openai_chat*` | `prompt_tokens`                                    | `prompt_tokens_details.cached_tokens` / `.cache_write_tokens` | `completion_tokens`                             | `completion_tokens_details.reasoning_tokens` |
| `google`       | `prompt_token_count + tool_use_prompt_token_count` | `cached_content_token_count` / not reported                   | `candidates_token_count + thoughts_token_count` | `thoughts_token_count`                       |

### Compact event

```json
{"role": "compact", "content": [{"type": "text", "text": "<summary>"}], "meta": {"provider": "...", "model": "...", "usage": {...}}}
```

The marker stores the summary and its request usage. Automatic compaction also includes that request in the turn's cumulative usage. See "Context Compaction" below.

### Rewind event

```json
{"role": "rewind", "meta": {"rewind_to": 5, "created_at": "..."}}
```

Marks an undo point. See "Rewind" below.

## Load Order

When `SessionStore.load_session` runs:

1. Read all JSONL lines into a raw list
2. `apply_rewind()` — scan sequentially; when a rewind record is found, truncate the accumulated list to `meta.rewind_to` and continue

`load_session` returns the raw timeline (minus rewound tails) as the visible history. `compact` records stay in place as inline markers — UIs render them as dividers, and the provider adapter substitutes the summary into provider context lazily on each request in `prepare_messages` (via `compact.apply_compact_replay`). Orphan `tool_use` blocks left by an interrupted run are closed by the provider adapter when the messages are replayed, not by the loader.

## Context Compaction

Checked after every completed assistant turn (with or without tools), always at
a full `assistant`/`tool_result` boundary.

1. `should_compact()` — true when the latest assistant message's `usage.total_tokens` ≥ `context_window × compact_threshold` (default `0.8`). Tool outputs appended this turn aren't reflected in that figure until the next API call's usage; the `(1 - threshold)` headroom absorbs them.
2. Ask the same provider/model for a summary with the normal system prompt, the current provider-projected messages (`prepare_messages`), no tools, text only, and `max_tokens = min(agent.max_tokens, 8192)`
3. Build a compact event with the summary text and the summary call's `meta.usage` when available
4. Persist the compact event and append it to `agent.messages` (append-only — original messages stay in JSONL and in the visible list)
5. Emit the `compact` stream event to the caller (empty payload — clients use it as the cue to insert their inline divider)

The headroom `(1 - compact_threshold) × context_window` is reserved for the
compact call itself: that call sends the full current history as input plus a
summary capped at `min(agent.max_tokens, 8192)`. Lowering
`compact_threshold` just triggers earlier; raising it is bounded by how much
headroom the compact call needs to fit.

If the summary request fails or returns no text, the agent logs a warning and keeps the full in-memory history. No `compact` record is persisted in that case, and the next threshold check will try compaction again — compaction is best-effort and never aborts the turn. A user-initiated cancel inside compaction is the one exception: it ends the turn immediately by emitting an `error` event with message `"cancelled"`, mirroring how phase 1 handles cancellation.

### Manual compaction

`Agent.acompact()` / `Agent.compact()` run steps 2–4 above on demand, ignoring the threshold in step 1. They share the same summary request and persistence path, so the on-disk record is identical to an automatic compaction. The one added precondition is `has_compactable_history()` — there must be a non-empty `user`/`assistant` message past the latest `compact` marker, or the call raises `NothingToCompactError` before any provider request. This is what stops a second immediate `/compact` from re-summarizing the previous summary. See `docs/sdk.md` for the public API and the CLI's `/compact` command.

### Provider projection

Visible state preserves pre-compact history and `compact` markers. Before each provider request, the provider adapter's `prepare_messages` (via `compact.apply_compact_replay`) rebuilds a provider-facing view:

- finds the last `compact` marker in `self.messages`
- replaces everything up to and including that marker with one synthetic `user` message that frames the continuation, embeds the summary text, and points at the original JSONL transcript
- drops any earlier `compact` markers from the tail
- shape of the synthetic head depends on the tail:
  - tail empty or starts with `assistant` → no ack; the summary `user` message ends with a resume instruction so the LLM continues directly
  - tail starts with `user` (a real follow-up prompt) → a short synthetic `assistant` ack (`Acknowledged.`) is inserted between the summary and the tail to preserve user/assistant alternation

The visible list seen by UIs and used by rewind never contains these synthetic substitutes — they exist only in the provider-projected messages produced by `prepare_messages`.

## Rewind

Triggered by `POST /api/chat` with `rewind_to`:

1. Server validates the target is a real user message
2. Server calls `append_rewind(session_id, rewind_to)` — appends a rewind marker to JSONL
3. Agent auto-resumes; `apply_rewind()` produces the truncated visible history

Rewind indices refer to the visible history, which now includes pre-compact turns and inline `compact` markers. Rewinding to a real user message before a `compact` marker slices the marker away along with the rest of the tail — the next provider request will then see the full pre-compact history again. The marker itself is never a valid rewind target.

## tool-output/ Spill

Bash output exceeding 5MB in memory (`_BASH_MAX_IN_MEMORY_BYTES`) is written to `<tool_output_dir>/bash-<tool_call_id>.log`. The tool result keeps the last 2000 lines in memory and cites the saved log path.

`tool_output_dir` is always set — Agent defaults it to a session-adjacent directory when persistence is configured, or a tempdir-scoped equivalent otherwise. The directory itself is created lazily on first spill.

Cancelled streaming tools persist emitted output plus `error: cancelled`.

## Session Store API

`SessionStore` (in `mycode/src/mycode/session.py`):

- `SessionStore(data_dir: Path)` — required; no default
- `session_exists(session_id)` — check by `meta.json` presence
- `create_session(session_id, *, cwd)` — write `meta.json` and touch `messages.jsonl`
- `list_sessions(*, cwd=None)` — filter by workspace, sorted by `updated_at` desc; derived `title` / `updated_at` included per entry
- `load_session(session_id)` — load with full replay pipeline (returns `None` when absent)
- `load_raw_messages(session_id)` — raw append-only JSONL, including rewound tails and markers; returns `[]` when absent
- `delete_session(session_id)` — recursive directory delete
- `clear_session(session_id)` — truncate `messages.jsonl`, reset `title` to the default and bump `updated_at`
- `append_message(session_id, message)` — append one line; refresh meta's `updated_at` and promote `title` on the first user message
- `append_rewind(session_id, rewind_to)` — append a rewind marker

All file I/O is offloaded to `asyncio.to_thread()`.
