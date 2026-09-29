# Web UI

The React + Vite UI in `web/src/`, served by the CLI server — serving modes and CORS rules live in `docs/api.md`. `pnpm --dir web dev` runs the Vite dev server against `mycode web --dev`, proxying `/api` to `http://localhost:8000`.

## Structure

```text
web/src/
  App.tsx                    # root layout, config loading, session init
  types.ts                   # API, message, and UI types
  components/
    Chat/
      Composer.tsx             # Lexical input, slash commands, and @ completion
      InputArea.tsx            # composer controls and uploads
      MessageList.tsx          # message windowing and scroll behavior
      MessageBubble.tsx        # message block rendering
      WorkSection.tsx          # folded turn work and its summary row
      ToolCard.tsx             # tool execution rendering
    Settings/                  # provider configuration editor
    ui/                        # shared UI primitives
    Sidebar.tsx                # sessions, workspace, and settings entry
    WorkspacePicker.tsx        # workspace browser
    SessionSearch.tsx          # ⌘K / Ctrl+K search over the workspace's sessions
  hooks/
    useChat.ts                 # chat state and SSE streaming
    useWorkspaceFiles.ts       # @ completion candidates
  utils/
    format.ts                  # cost, duration, path, and date formatting
    platform.ts                # macOS detection for shortcut modifiers
    messages.ts                # canonical messages → render messages
    completion.ts              # slash, skill, and @ token matching
    config.ts                  # local and remote config resolution
    storage.ts                 # browser persistence
```

Tests live beside the code they cover. `src/test/setup.ts` contains the shared Vitest setup.

## Message State Model

`useChat.ts` keeps four pieces of reducer state:

- `rawMessages: ChatMessage[]` — canonical block messages (mirrors the JSONL timeline; includes `role: "compact"` markers)
- `toolRuntimeById` — ephemeral tool runtime state (streaming output, pending flags, final result)
- `sessionUsage` — session token and cost totals from session load or the latest SSE `usage`; `null` when unknown
- `pending: {steers, queue}` — messages handed to the running chat and not yet delivered. Each `PendingInput` has a client-generated `id` (sent as `input_id`), the composer submission and uploads for restoring it, and the request `input` blocks. Loading a session sets it from the snapshot's `pending` (id from `meta.input_id`, typed text only; an item whose message carries attachment blocks is `partial`, since the composer cannot rebuild what the server built); a session switch clears it

The render-ready list `messages: RenderMessage[]` (where `RenderMessage = ChatMessage | CompactMarkerMessage`) is derived via `useMemo(buildRenderMessages(rawMessages, toolRuntimeById))`. There is no second copy of state to keep in sync — every reducer transition produces a new `rawMessages` and/or `toolRuntimeById` reference and the projection is recomputed.

`CompactMarkerMessage` (`{kind: "compact-marker", sourceIndex, renderKey}`) carries no content of its own — it just tells `MessageList` to render `CompactMarker` instead of `MessageBubble`. Use the `isCompactMarker(msg)` type guard from `types.ts` to narrow when iterating. Only manual and untagged compact markers become `CompactMarkerMessage`s; an automatic one (`meta.trigger: "auto"`) belongs to its turn and becomes a render-only `{type: "compact"}` block in that turn's bubble.

The reducer runs eagerly into a ref that mirrors React state, so request and stream code reads the current pending list rather than the last render's. Actions:

- `set_messages` — load session history from server
- `start_turn` — optimistic user message + empty assistant
- `rewind_and_start_turn` — rewind + optimistic new turn
- `apply_event` — apply one SSE event to `rawMessages` / `toolRuntimeById`; `user_message` appends the message plus an empty assistant, the shape `start_turn` creates, and drops the pending items listed in `meta.input_ids`
- `rollback` — restore the snapshot taken before an optimistic turn
- `add_pending` / `move_pending` / `remove_pending` / `clear_pending` — edit the pending lists

`buildRenderMessages()` in `utils/messages.ts` is the single projection used by both initial load and live streaming. A turn runs from a real user message to the next one and renders as one assistant bubble: tool results visually attach to their `tool_use`, the assistant messages of a tool loop merge, and automatic compaction stays inside the turn. A live `compact` SSE event appends a `{role: "compact", meta: {trigger}}` entry to `rawMessages`, which the next render projects the same way as the persisted marker.

`buildRenderMessages()` also derives `TurnStats` for each turn's bubble:

- History sums persisted per-request `usage` and `cost`. Missing costs are skipped; any total-only request downgrades the turn cost to total-only.
- Streaming uses the latest cumulative `turn_usage`, `turn_cost`, and `turn_duration_ms` without summing prior events. Missing fields clear stale values.
- History `duration_ms` runs from the opening user message's `meta.created_at` to that of the turn's last completed assistant or automatic compact marker, the records the SDK sends `usage` for. A segment followed by a `meta.steer` user message runs to that message's `created_at`, matching the SDK's closing `usage` event. Partial responses (`stop_reason` `error` / `cancelled`) are skipped, so a reload matches the streamed value. A streamed `turn_duration_ms` wins over timestamps: a reattached run's history ends before the records still streaming.
- Automatic compaction bills its summary request to the turn and leaves the context occupancy unknown until the next request. Manual markers stand alone and add nothing to a turn.
- `null` means unknown and is omitted by the UI. The Web never resolves model pricing.
- A delivered steer starts a new turn in the projection. Its `user_message` appends a fresh assistant, and `usage` events patch the latest assistant with cumulative values, so the new segment's stats come only from the events after it. Reconnect replay rebuilds the same shape from the snapshot's history and `pending_events` without summing anything twice.

Usage stats (`components/Chat/StatsCard.tsx`):

- The composer shows `context % · session cost`; each assistant footer shows `model · turn cost`. On mobile the composer drops the cost when a percentage is shown.
- Either opens a card on hover or tap. `UsageGrid` lists Input (excluding cache), Cache read, Cache write, Output (including reasoning), and Total. A known cost adds a cost column: filled per row when the cost has a breakdown, always on Total.
- The session card adds `Context` and `Cache hit` (cache read ÷ input, only when the provider reports caching) above the table.
- `currentContext` is the latest context occupancy after the last compact marker.
- The percentage turns `destructive` at 90% of the `compact_threshold` from `GET /api/config`, or of the window when auto-compact is off.

Key design decisions:

- Tool results persisted as `user` messages with `tool_result` blocks are visually folded into the preceding assistant message during rendering
- Each render message and block gets a stable `renderKey` for React reconciliation
- `sourceIndex` tracks the original message position; rewind uses this index against the visible list, so rewinding to a real user message before a `compact` marker slices the marker away too

Rendering rules:

- `thinking` blocks → `ReasoningBlock` (expanded while streaming, uses `meta.duration_ms` when present)
- `tool_use` blocks → `ToolCard` (with matching `tool_result` and live runtime folded in)
- `text` blocks → `MarkdownBlock`. The last block of the streaming bubble gets `streaming`, which runs `remend` to close unterminated `**`, `` ` `` and `$$` before parsing and to show an unfinished link as its text alone (remend's placeholder URL would render as a link to the current page). Finished text renders as written: remend misreads complete text such as `./src/**/*`.
- `image` blocks → inline image preview in `MessageBubble`
- `compact` blocks and `compact-marker` entries → `CompactMarker` (a thin labelled divider, no interactivity)
- `meta.error` on a `stop_reason: "error"` assistant → one plain-text `text-destructive` line at the end of the bubble, not markdown and not part of the copied text

Turn work folding (`WorkSection.tsx`, `splitTurn()` in `utils/messages.ts`):

- `splitTurn()` splits an assistant turn into its work, its answer (the trailing text), and an automatic compaction after the answer.
- While the turn runs, `WorkSection` renders the work open with no summary row, so a first tool call inserts nothing. It holds the work from the first block on, so folding never remounts the work or the answer.
- A finished turn folds when its work has a tool call or automatic compaction, whether it completed, stopped or failed. Otherwise it stays flat.
- `buildRenderMessages()` sets the bubble's `interruption` from the turn's last record: a response with `stop_reason` `cancelled` or `error` gives that value; tool results with no response after them give `cancelled`, since the SDK persists nothing for a stop between rounds, unless the next message is a `meta.steer` user message: then the turn continued with the steer, `interruption` stays unset, and its summary row reads like a finished turn. A steer message renders as an ordinary user bubble. Live `error` and `cancelled` events mark the tail assistant the way the SDK marks the partial it persists. An error before any assistant record is live only, so after a reload that turn reads as `cancelled`.
- Folding fades in one summary row, `Worked for 23s · 2 edits · 1 command · 5 reads · 1 failed`, and collapses the body, including a compaction after the answer, with the same grid-rows and opacity transition as `ReasoningBlock`. A history turn mounts its work the first time it opens and keeps it.
- An interrupted turn's row leads with `Stopped` (muted) or `Failed` (`text-destructive`) instead of a duration, which would run only to the last completed response. The error line stays below the fold. A stopped turn with nothing to fold ends with a muted `Stopped` line.
- Tools are counted by name, side effects first; `failed` counts error results except the SDK's `error: cancelled`. The counts show whole or not at all: `SummaryRow` measures the full row against its space with a `ResizeObserver` and drops them when it would not fit on one line. `aria-label` always carries the full summary.
- Copy takes the answer text only.

`MessageList` renders long histories as a tail window: initial session load renders the latest messages and scrolls to the bottom before paint. Scrolling near the top prepends older messages in batches and preserves the current viewport by restoring the previous distance from the bottom. Auto-scroll follows incoming message updates only while the user is already near the bottom; local height changes such as expanding tools do not trigger it. Following stops when the user scrolls up away from the bottom and resumes near it. Away from the bottom, including after content grows without a scroll, a round arrow button fades in; clicking it follows again and smooth-scrolls to the latest output.

The fold commits when the streaming assistant stops streaming: at the end of the run, or mid-run when a `user_message` appends a new segment while `loading` stays true. `SettleAnchor` takes the streaming assistant's render key, reads the reader's position in `getSnapshotBeforeUpdate` when that key changes from a value, and, if the work folded, holds it until the fold's transitions end:

- reading below the work: the work's bottom edge, and so the answer, stays put;
- reading inside the work: the summary row moves to the top edge;
- reading above the work: nothing moves;
- following the bottom: stays at the bottom.

Any scroll input (wheel, touch, pointer, key) ends the hold. Manual toggles need no correction: the body sits below the row that was clicked.

## Streaming

`useChat.ts` follows the `docs/api.md` contract: `POST /api/chat` returns `{run, session}`; `GET /api/runs/{run_id}/stream` feeds each `data:` line into the reducer as a `StreamEvent`; `data: [DONE]` ends the stream. On disconnect the UI reloads via `GET /api/sessions/{id}`; a 409 on send attaches to the existing run's stream.

A live `compact` SSE event is consumed by the reducer at the position it arrives — the marker lands between whatever just streamed and whatever streams next, mirroring where the agent emitted it (e.g. between two tool calls of the same turn). The server has already persisted the `compact` JSONL record at the same point with the same `trigger`, so a later session reload renders the same result without any extra round-trip.

Steer and queue (`POST /api/runs/{id}/steer`, `POST /api/sessions/{id}/queue`, `DELETE /api/sessions/{id}/queue/{input_id}` in `docs/api.md`):

- `steer()` adds the item to `pending.steers` and posts it to the active chat run. A `409` moves it to the queue and calls the queue path.
- `queue()` adds the item to `pending.queue` and posts it. A `409` keeps it in the local queue only; if the stream has already ended it is sent at once.
- Any other failure, including a network error, takes the item out, restores it to the composer, and sets `sendError`. A network error is never read as a rejection.
- Each step of that chain first checks the item is still pending. A stop, a stream end, a delivery or a session switch takes items out, and from then on the taker owns them, so a late `409` after Stop never queues or sends anything.
- `removeQueued()` deletes the item and drops it locally on `200` or `404`. `takeBackQueued()` deletes it and returns it for the composer, or `null` on `404` (delivered, or waiting to be sent when the run ends). `steerQueued()` posts `/api/sessions/{id}/queue/{input_id}/steer`: the server moves the message it built, so a reloaded item keeps its attachments. `200` moves the item to `pending.steers`; `404` and `409` leave it alone, since the item is on its way or becomes the next turn.
- A queue request answered `200` after the item was removed locally deletes it from the server again, so Remove during the request does not leave a message behind.
- Steer, queue and move requests in flight are tracked in `inflightRef`. A stream end waits for them before deciding what is still pending, so a request the old run rejected joins the leftovers instead of racing the next `/api/chat`. The run stays `loading` until then, so a manual send cannot race the leftovers' own request either.

When a chat stream ends, the pending lists are emptied in one step:

- `[DONE]` with no `cancelled` or `error` before it: every item still pending, steers then queue, goes out as one `/api/chat` request with their `input` blocks concatenated. This covers an item submitted as the run finished; the server leaves the session slot before `[DONE]`. If that send is rejected, even with `409`, the items go back to the composer and `sendError` is set.
- `cancelled`, `error` (live or in replayed `pending_events`), or a disconnect that cannot be recovered: every item goes back to the composer, steers then queue, blank-line separated, ahead of the current draft; uploads go back into the attachment list ahead of the current ones. Stop aborts the stream before its `cancelled` arrives, so `cancel()` takes the items itself and restores them once the session reloads, skipping any whose id the reloaded history lists in `meta.input_ids`: a steer committed just before the stop that this client had not yet seen. A session switched to during that reload keeps its own composer; the items are dropped.
- A disconnect that reloads the session takes the snapshot's `pending` instead. Items the server never accepted are lost there.

`useChat` hands restored items to its `onRestore` callback; `App` puts the text into the composer through `InputArea`'s `prepend` handle, which rebuilds `@path` pills from the submission's references.

`permission_request` opens the approval prompt and `permission_resolved` clears it. `cancelled` clears pending permissions and tool activity without adding an error message.

A chat `error` event adds no content. `markTailAssistantStopped()` sets `meta.stop_reason: "error"` and `meta.error` (the event message, else `Unknown error`) on the tail assistant, creating an empty one when the tail is not an assistant, so an error before any output still has a bubble. When the SDK persisted a failed assistant record it carries the same `meta.error`, so a reload renders the same row; an error before any assistant record (a 401 before output, a failure between tool rounds) shows live only. A rejected send is a request failure, not a turn outcome: it rolls back and sets `sendError`, which `MessageList` renders at the tail in the same style as the error line. The next send, rewind, compaction or session switch clears it.

Streaming state tracking:

- `streamTokenRef` — incremented to invalidate stale streams
- `pendingRequestTokenRef` — deduplicates concurrent send requests
- `activeRunRef` — tracks the current run for cancel
- `runKind: "chat" | "compact" | null` — kind of the run being followed; `loading` derives from it, and `MessageList` treats the tail assistant as streaming only for chat runs

Manual compaction (`/compact`):

- `compactSession()` posts `POST /api/sessions/{id}/compact` with the active provider/model and streams the returned `kind: "compact"` run through the normal SSE reader. No optimistic user/assistant message is created; a 409 attaches to the existing run using its `kind`.
- Compact runs send only `compact` to the reducer; errors set `compactError`; `cancelled` is a stop and does not. On completion, the UI reloads persisted history and session cost. Pending-event replay follows the same routing.
- Compacting feedback lives in the message area, not the toolbar: while a compact run is active, `MessageList` renders a pending `CompactMarker` (same divider geometry, pulsing `compacting…` label) at the tail, which settles into the real `compacted` divider when the marker arrives. Failures render a quiet inline note (`compaction failed` / `nothing to compact`, full detail in `title`) in the same position from `compactError`, which clears on the next run or session change. The input area only reflects the shared busy state (disabled composer + stop button).

Composer and attachments:

- While a chat run is active the composer stays enabled; during a compact run nothing can be sent. Enter steers the running turn; `⌘/Ctrl+Enter` or `⌘/Ctrl`+click on the send button queues the message for the next turn. Idle, both send normally. `App.handleSubmit()` routes to `send`, `steer` or `queue`.
- One button sits in the send slot: with text, workspace refs or uploads it is the send arrow, whose `title` while running names both deliveries (`Steer · Enter, queue · ⌘Enter`); with an empty composer while running it is Stop.
- Pending steers render at the tail of `MessageList` as hollow user bubbles (`MessageBubble` `pending`: hairline outline, no fill, 80% text) with the sidebar's breathing accent dot on their left, until their `user_message` replaces them with the real, filled bubble. The state is in the bubble itself; `title` and visually hidden text say `Waiting for the current step…`.
- Queued messages are a card behind the composer (`mx-4`, top corners rounded, `bg-muted`, hairline shadow, `-mb-3` so the composer covers its bottom edge), one row per item in order: a `CornerDownRight` glyph, the first line of the text with the full text in `title`, an attachment count after a paperclip when there are any, and always-visible muted actions: `Steer` (icon and label), Edit and Remove (icons). No enter or leave motion; nothing renders when the queue is empty. Edit puts the message back into an empty composer; with content in it, the toolbar notice reads `Clear the composer to edit`. A `partial` item has no Edit, since taking it back would drop the attachments the server holds.
- Esc while a run is active cancels it, the same path as the composer's stop button. The handler lives in `App.tsx` and yields when the event is already `defaultPrevented` (permission prompt denies, completion menu closes, message edit closes), when an IME composition is active, or when a `[role=dialog]` (settings sheet) is in the event path.
- ⌘K (macOS) / Ctrl+K opens `SessionSearch`, also reachable from the search button beside `+` in the sidebar. The handler lives in `App.tsx` because the mobile sidebar is unmounted while its drawer is closed; it yields like the Esc handler (`defaultPrevented`, IME composition, `[role=dialog]` in the path). Results come from `GET /api/sessions/search`.
- `Composer` (Lexical) is the single source of truth for message text + inline `@` refs; submit hands `useChat.send` a `ComposerSubmission = { text, workspaceFiles }` and `useChat` builds the `input` blocks (workspace refs deduped by `kind + path`, uploads appended).
- `WorkspaceFileNode` pills serialize as `@path` inside the message text; the file content travels separately as a `path` input block — both must stay consistent with the CLI `@file` behavior.
- Built-in slash commands match a whole-input token while the composer is idle with an empty upload list. Skill and `@` completion also work while a run is active, so a `/skill` token can be steered. Skills from `GET /api/config` complete as editable `/<skill-name>` text at any standalone slash token. The backend expands exact discovered names; other slash tokens are submitted as text.
- Skill snapshot text blocks (`meta.skill_snapshot=true`) remain in `rawMessages` for provider replay. `buildRenderMessages()` gives history, copy, and edit the original user text.
- ArrowUp recalls previously sent prompts only from an empty composer (or while already recalling); ArrowDown walks forward and past the newest entry clears the editor, and any edit leaves recall. `InputArea` stores accepted prompt texts per workspace (`mycode_prompt_history`, keyed by `cwd`, capped at 30, consecutive duplicates skipped); pills come back as plain `@path` text, and an open completion menu keeps the arrow keys.
- `@` completion uses `GET /api/workspaces/files`. Refs the model can't ingest block submit with a hint — never silently drop a pill (it would break the sentence).
- Optimistic workspace refs render as empty-data file cards; after reload the server-persisted blocks render instead (workspace image: card live, real preview after reload — intentional).
- Upload attachments (picker/drag/paste) stay in `InputArea`: text as inline snapshot blocks and media as base64 blocks. Unsupported additions briefly replace the effort pill with a compact toolbar notice; attachments already in the draft survive model changes and block submit with a persistent notice until removed or supported again, matching inline `@` refs.

## Configuration

The Web UI persists these values to `localStorage`:

- `provider`, `model`, `cwd`, and `reasoningEfforts` keyed by `provider/model`
- A stored provider that is no longer available falls back to the `default` provider from `GET /api/config`; a stored model the selected provider no longer lists falls back to its first model
- A missing model-specific effort uses `auto`; `auto` is stored explicitly when selected
- recent workspaces, active sessions, sidebar width, and theme

The input-area effort selector uses `providers[name].reasoning_efforts[model]`; it renders only when
the provider supports effort and the model has non-empty values. Settings editor options come from
`provider_type_env_vars` and `provider_type_default_models`.

## Development

```bash
pnpm --dir web install                                 # install dependencies
pnpm --dir web check                                   # Biome lint and format check
pnpm --dir web typecheck                               # TypeScript type checking
pnpm --dir web test                                    # run web UI tests
pnpm --dir web dev                                     # dev server (Vite HMR)
pnpm --dir web build                                   # production build to web/dist/
```

## Packaging

```bash
uv build --package mycode-cli
```

`web/dist/` is not the serving path. `cli/hatch_build.py` copies it into
`cli/src/mycode_cli/server/static/` during package builds. Editable installs skip this build step.

If `cli/src/mycode_cli/server/static/` is missing at startup, the server runs in API-only mode and
logs a warning.
