# Sessions

Sources: `mycode/src/mycode/session.py`, `cli/src/mycode_cli/sessions.py`

## Storage Layout

```text
<data_dir>/
  <session_id>/
    meta.json      # CLI-owned catalog entry
    messages.jsonl # SDK-owned append-only timeline
    tool-output/   # CLI tool spill files, created lazily
```

The SDK owns only `messages.jsonl` and does not know about workspaces, titles, timestamps, or tool output. Applications may keep their own files beside the timeline. The CLI adds `meta.json` and `tool-output/` and resolves `data_dir` to `$MYCODE_HOME/sessions/` (default `~/.mycode/sessions/`).

The SDK timeline appears on the first persisted message. The CLI normally creates its catalog entry on the first user turn; the explicit `POST /api/sessions` endpoint creates an empty `"New chat"` entry immediately.

## meta.json

```json
{
  "cwd": "/path/to/workspace",
  "title": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

- `cwd` — CLI workspace, used for session filtering and restoration
- `title` — first readable user text, flattened and truncated to 48 characters; `"New chat"` until then
- `created_at` / `updated_at` — CLI catalog timestamps

`updated_at` tracks user-visible session changes: a user turn, rewind, clear, or successful manual compact. Provider/tool messages within a turn do not update the catalog separately. Clearing a session also resets its title to `"New chat"`.

The CLI lists sessions by scanning valid `meta.json` files, optionally filters by `cwd`, and sorts by `updated_at` descending. Per-turn provider/model state remains on each `ConversationMessage.meta`.

## messages.jsonl Record Types

Each line is a JSON object. The `role` field acts as a discriminator.

### Regular message

Standard `user` or `assistant` message in the internal block format.

```json
{"role": "user", "content": [{"type": "text", "text": "..."}], "meta": {...}}
{"role": "assistant", "content": [{"type": "thinking", "text": "...", "meta": {"duration_ms": 1200}}, {"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}], "meta": {"provider": "...", "model": "...", "stop_reason": "...", "usage": {"total_tokens": 1456, "input_tokens": 1400, "cache_read_tokens": 1200, "cache_write_tokens": 100, "output_tokens": 56, "reasoning_tokens": 20}, "cost": {"input": 0.0001, "cache_read": 0.0002, "cache_write": 0.0001, "output": 0.0008, "reasoning": 0.0003, "total": 0.0015}, "context_window": 200000}}
```

`assistant.meta.stop_reason` uses the canonical values `stop`, `tool_use`, `length`, `error`, `cancelled`, and `unknown`. Provider adapters convert their native finish reasons before persistence. `assistant.meta.model` records the selected request model, not a provider-returned alias or routed model name. A streamed tool block with unparseable JSON carries `meta.invalid_input=true` plus the original text in `meta.raw_arguments` and the parser message in `meta.parse_error`.

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

`usage.total_tokens` is the request's context metric. Compaction and context displays compare it with `context_window`.

`assistant.meta.cost` is the fixed USD cost recorded when the request completes. SDK estimates contain `input`, `output`, optional cache/reasoning components, and `total`. Provider-reported totals contain only `total`. Missing cost means the request could not be priced.

`assistant.meta.context_window` is the effective model context window for the request.

Failed or cancelled streams may persist partial assistant content with `stop_reason="error"` or `"cancelled"` and no `usage` because final usage never arrived.

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

When the SDK's `SessionStore.load_messages` runs:

1. Read all JSONL lines into a raw list
2. `apply_rewind()` — scan sequentially; when a rewind record is found, truncate the accumulated list to `meta.rewind_to` and continue

`load_messages` returns the timeline minus rewound tails as visible history. `compact` records stay in place as inline markers — UIs render them as dividers, and the provider adapter substitutes the summary into provider context lazily on each request in `prepare_messages` (via `compact.apply_compact_replay`). Orphan `tool_use` blocks left by an interrupted run are closed by the provider adapter when messages are replayed, not by the loader.

## Context Compaction

Checked after every completed assistant turn (with or without tools), always at
a full `assistant`/`tool_result` boundary.

1. `should_compact()` — true when the latest assistant message's `usage.total_tokens` ≥ `context_window × compact_threshold` (default `0.8`). Tool outputs appended this turn aren't reflected in that figure until the next API call's usage; the `(1 - threshold)` headroom absorbs them.
2. Ask the same provider/model for a summary with the normal system prompt, the current provider-projected messages (`prepare_messages`), no tools, text only, and `max_tokens = min(agent.max_tokens, 8192)`
3. Build a compact event with the summary text, `meta.usage`, `meta.cost`, and `meta.context_window`
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

## tool-output/ Logs

The CLI creates `CliDeps(cwd, tool_output_dir)` for each agent and passes it through `Agent(deps=...)`. Bash writes output exceeding its 2000-line or 50KB display limit as raw combined stdout/stderr to `<tool_output_dir>/bash-<tool_call_id>.log`; webfetch uses the same directory for truncated converted content. Tool results cite the saved path.

The CLI sets `tool_output_dir` to `<data_dir>/<session_id>/tool-output/`. The tools create it only when output spills. The SDK has no output-directory policy.

Cancelled bash calls persist the captured final tail plus `error: cancelled`; truncated results also cite the raw log. Live `tool_output` events are not session data.

## Store APIs

SDK `SessionStore` (`mycode/src/mycode/session.py`) manages only the timeline:

- `SessionStore(data_dir: Path)` — required; no default
- `session_exists(session_id)` — check whether `messages.jsonl` exists
- `load_messages(session_id)` — visible history after applying rewind markers
- `load_raw_messages(session_id)` — raw append-only JSONL, including rewound tails and markers; returns `[]` when absent
- `append_message(session_id, message)` — append one line, creating the session directory lazily
- `append_rewind(session_id, rewind_to)` — append a rewind marker
- `clear_messages(session_id)` — truncate an existing timeline

CLI `SessionStore` (`cli/src/mycode_cli/sessions.py`) extends the SDK store with catalog and application lifecycle operations:

- `create_session(session_id, *, cwd)` — create a `"New chat"` catalog entry without creating a timeline
- `record_user_turn(session_id, *, cwd, text)` — lazily create metadata, derive the first title, and update activity time
- `touch(session_id)` — update activity time after rewind or successful manual compact
- `load_session(session_id)` — load catalog metadata and visible messages
- `list_sessions(*, cwd=None)` / `latest_session(...)` — scan and sort the catalog
- `clear_session(session_id)` — clear the timeline, reset the title, and update activity time
- `delete_session(session_id)` — remove catalog, timeline, and tool output together

All file I/O is offloaded to `asyncio.to_thread()`.
