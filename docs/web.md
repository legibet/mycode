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
  hooks/
    useChat.ts                 # chat state and SSE streaming
    useWorkspaceFiles.ts       # @ completion candidates
  utils/
    messages.ts                # canonical messages → render messages
    completion.ts              # slash, skill, and @ token matching
    config.ts                  # local and remote config resolution
    storage.ts                 # browser persistence
```

Tests live beside the code they cover. `src/test/setup.ts` contains the shared Vitest setup.

## Message State Model

`useChat.ts` keeps three pieces of reducer state:

- `rawMessages: ChatMessage[]` — canonical block messages (mirrors the JSONL timeline; includes `role: "compact"` markers)
- `toolRuntimeById` — ephemeral tool runtime state (streaming output, pending flags, final result)
- `sessionCost` — session cost from session load or the latest SSE `usage`; `null` is hidden

The render-ready list `messages: RenderMessage[]` (where `RenderMessage = ChatMessage | CompactMarkerMessage`) is derived via `useMemo(buildRenderMessages(rawMessages, toolRuntimeById))`. There is no second copy of state to keep in sync — every reducer transition produces a new `rawMessages` and/or `toolRuntimeById` reference and the projection is recomputed.

`CompactMarkerMessage` (`{kind: "compact-marker", sourceIndex, renderKey}`) carries no content of its own — it just tells `MessageList` to render `CompactMarker` instead of `MessageBubble`. Use the `isCompactMarker(msg)` type guard from `types.ts` to narrow when iterating. Only manual and untagged compact markers become `CompactMarkerMessage`s; an automatic one (`meta.trigger: "auto"`) belongs to its turn and becomes a render-only `{type: "compact"}` block in that turn's bubble.

State is managed via `useReducer` with actions:

- `set_messages` — load session history from server
- `start_turn` — optimistic user message + empty assistant
- `rewind_and_start_turn` — rewind + optimistic new turn
- `apply_event` — apply one SSE event to `rawMessages` / `toolRuntimeById`
- `rollback` — restore the snapshot taken before an optimistic turn

`buildRenderMessages()` in `utils/messages.ts` is the single projection used by both initial load and live streaming. A turn runs from a real user message to the next one and renders as one assistant bubble: tool results visually attach to their `tool_use`, the assistant messages of a tool loop merge, and automatic compaction stays inside the turn. A live `compact` SSE event appends a `{role: "compact", meta: {trigger}}` entry to `rawMessages`, which the next render projects the same way as the persisted marker.

`buildRenderMessages()` also derives `TurnStats` for each turn's bubble:

- History sums persisted per-request `usage` and `cost`. Missing costs are skipped; any total-only request downgrades the turn cost to total-only.
- Streaming uses the latest cumulative `turn_usage`, `turn_cost`, and `turn_duration_ms` without summing prior events. Missing fields clear stale values.
- History `duration_ms` runs from the opening user message's `meta.created_at` to that of the turn's last completed assistant or automatic compact marker, the records the SDK sends `usage` for. Partial responses (`stop_reason` `error` / `cancelled`) are skipped, so a reload matches the streamed value. A streamed `turn_duration_ms` wins over timestamps: a reattached run's history ends before the records still streaming.
- Automatic compaction bills its summary request to the turn and leaves the context occupancy unknown until the next request. Manual markers stand alone and add nothing to a turn.
- `null` means unknown and is omitted by the UI. The Web never resolves model pricing.

The composer shows `context % · session cost`; assistant footers show model and turn cost. `currentContext` uses the latest post-compact context. Mobile shows only the percentage.

Key design decisions:

- Tool results persisted as `user` messages with `tool_result` blocks are visually folded into the preceding assistant message during rendering
- Each render message and block gets a stable `renderKey` for React reconciliation
- `sourceIndex` tracks the original message position; rewind uses this index against the visible list, so rewinding to a real user message before a `compact` marker slices the marker away too

Rendering rules:

- `thinking` blocks → `ReasoningBlock` (expanded while streaming, uses `meta.duration_ms` when present)
- `tool_use` blocks → `ToolCard` (with matching `tool_result` and live runtime folded in)
- `text` blocks → `MarkdownBlock`
- `image` blocks → inline image preview in `MessageBubble`
- `compact` blocks and `compact-marker` entries → `CompactMarker` (a thin labelled divider, no interactivity)

Turn work folding (`WorkSection.tsx`, `splitTurn()` in `utils/messages.ts`):

- `splitTurn()` splits an assistant turn into its work, its answer (the trailing text), and an automatic compaction after the answer.
- While the turn runs, `WorkSection` renders the work open with no summary row, so a first tool call inserts nothing. It holds the work from the first block on, so folding never remounts the work or the answer.
- A finished turn folds when it has an answer, a tool call or automatic compaction, and a last response that is not interrupted (`stop_reason` `error` / `cancelled`). Otherwise it stays flat. Live `error` and `cancelled` events mark the tail assistant the way the SDK marks the partial it persists.
- Folding fades in one summary row, `Worked for 23s · 2 edits · 1 command · 5 reads · 1 failed`, and collapses the body, including a compaction after the answer, with the same grid-rows and opacity transition as `ReasoningBlock`. A history turn mounts its work the first time it opens and keeps it.
- Tools are counted by name, side effects first, at most three segments plus `+N more` for the remaining calls. `failed` counts error results except the SDK's `error: cancelled`.
- Copy takes the answer text only.

`MessageList` renders long histories as a tail window: initial session load renders the latest messages and scrolls to the bottom before paint. Scrolling near the top prepends older messages in batches and preserves the current viewport by restoring the previous distance from the bottom. Auto-scroll follows incoming message updates only while the user is already near the bottom; local height changes such as expanding tools do not trigger it. Following stops when the user scrolls up away from the bottom and resumes near it. Away from the bottom, including after content grows without a scroll, a round arrow button fades in; clicking it follows again and smooth-scrolls to the latest output.

The fold commits with the end of streaming, so `SettleAnchor` reads the reader's position in `getSnapshotBeforeUpdate` and, if the work folded, holds it until the fold's transitions end:

- reading below the work: the work's bottom edge, and so the answer, stays put;
- reading inside the work: the summary row moves to the top edge;
- reading above the work: nothing moves;
- following the bottom: stays at the bottom.

Any scroll input (wheel, touch, pointer, key) ends the hold. Manual toggles need no correction: the body sits below the row that was clicked.

## Streaming

`useChat.ts` follows the `docs/api.md` contract: `POST /api/chat` returns `{run, session}`; `GET /api/runs/{run_id}/stream` feeds each `data:` line into the reducer as a `StreamEvent`; `data: [DONE]` ends the stream. On disconnect the UI reloads via `GET /api/sessions/{id}`; a 409 on send attaches to the existing run's stream.

A live `compact` SSE event is consumed by the reducer at the position it arrives — the marker lands between whatever just streamed and whatever streams next, mirroring where the agent emitted it (e.g. between two tool calls of the same turn). The server has already persisted the `compact` JSONL record at the same point with the same `trigger`, so a later session reload renders the same result without any extra round-trip.

`permission_request` opens the approval prompt and `permission_resolved` clears it. `cancelled` clears pending permissions and tool activity without adding an error message.

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

- Esc while a run is active cancels it, the same path as the composer's stop button. The handler lives in `App.tsx` and yields when the event is already `defaultPrevented` (permission prompt denies, completion menu closes, message edit closes), when an IME composition is active, or when a `[role=dialog]` (settings sheet) is in the event path.
- `Composer` (Lexical) is the single source of truth for message text + inline `@` refs; submit hands `useChat.send` a `ComposerSubmission = { text, workspaceFiles }` and `useChat` builds the `input` blocks (workspace refs deduped by `kind + path`, uploads appended).
- `WorkspaceFileNode` pills serialize as `@path` inside the message text; the file content travels separately as a `path` input block — both must stay consistent with the CLI `@file` behavior.
- Built-in slash commands match a whole-input token while the composer is idle with an empty upload list. Skills from `GET /api/config` complete as editable `/<skill-name>` text at any standalone slash token. The backend expands exact discovered names; other slash tokens are submitted as text.
- Skill snapshot text blocks (`meta.skill_snapshot=true`) remain in `rawMessages` for provider replay. `buildRenderMessages()` gives history, copy, and edit the original user text.
- `@` completion uses `GET /api/workspaces/files`. Refs the model can't ingest block submit with a hint — never silently drop a pill (it would break the sentence).
- Optimistic workspace refs render as empty-data file cards; after reload the server-persisted blocks render instead (workspace image: card live, real preview after reload — intentional).
- Upload attachments (picker/drag/paste) stay in `InputArea`: text as inline snapshot blocks and media as base64 blocks. Unsupported additions briefly replace the effort pill with a compact toolbar notice; attachments already in the draft survive model changes and block submit with a persistent notice until removed or supported again, matching inline `@` refs.

## Configuration

The Web UI persists these values to `localStorage`:

- `provider`, `model`, `cwd`, and `reasoningEfforts` keyed by `provider/model`
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
