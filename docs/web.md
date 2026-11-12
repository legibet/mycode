# Web UI

The React + Vite UI in `web/src/`, served by the CLI server — serving modes and CORS rules live in `docs/api.md`. `pnpm --dir web dev` runs the Vite dev server against `mycode web --dev`, proxying `/api` to `http://localhost:8000`.

## Structure

```text
web/src/
  App.tsx                # layout, config and settings loading, session list, transcript dialog state, global shortcuts
  api.ts                 # HTTP wire types, fetchJson / readApiError, SSE and /api/events readers
  types.ts               # message, render, and UI types
  components/
    composer/            # InputArea and the Lexical Composer, uploads, in-flight card and jobs bar, permission prompt
    transcript/          # TranscriptDialog: a subagent's transcript
    messages/            # MessageList, MessageBubble, WorkSection, ToolCard, markers, StatsCard, live log, SessionLinksContext
    markdown/            # MarkdownBlock, CodeBlock, Shiki highlighting, EditDiff
    settings/            # SettingsPanel, ProviderCard, config ⇄ form draft mapping
    sidebar/             # Sidebar, WorkspacePicker, SessionSearch
    ui/                  # shadcn primitives plus Collapse and ResponsiveDialog
  hooks/
    useChat.ts           # sessions, requests, and the main view's runs
    useSessionStream.ts  # snapshot-plus-stream core shared by useChat and useAgentSession
    useAgentSession.ts   # a subagent session for the transcript dialog
    chatState.ts         # pure chat state: reducer, pending items, run event routing
  utils/
    messages.ts          # canonical messages → render messages, turn stats, current context
    storage.ts           # localStorage persistence
```

Components are grouped by the area of the screen they belong to. Pure logic sits in a `.ts` file next to its component; tests sit beside the code they cover, with shared Vitest setup in `src/test/setup.ts`.

## UI Conventions

- Primitives in `components/ui/` are shadcn's `base-nova` style on Base UI: custom triggers use the `render` prop, and the files are formatted and linted like the rest of the code. Build controls from them rather than from raw elements, and prefer a Button variant over class overrides; `ghost-destructive` is the quiet button that turns red on hover. Settings rows use `Field`, which ties the visible label to its control.
- Theme tokens live in `index.css`: light values on `:root`, dark on `.dark`, with shadcn's names. `primary` is the brand blue and `accent` a quiet hover surface. Components use the semantic tokens and no `dark:` color overrides.
- Type scale: `text-2xs` (11px, micro labels), `text-xs` (secondary), `text-ui` (13px, rows and controls), `text-sm` (content). `utils/cn.ts` registers the custom sizes with `tailwind-merge`, which would otherwise read them as colors.
- One breakpoint, `md` (768px), read in code through `useIsDesktop()`. Below it the sidebar becomes a drawer and `ResponsiveDialog` renders dialogs as bottom sheets.

## Message State

`useChat` keeps the shown session in a reducer (`chatState.ts`), through `useSessionStream`:

- `rawMessages` — canonical block messages, mirroring the JSONL timeline including `compact` markers
- `toolRuntimeById` — live tool state: streaming output, pending flags, the final result
- `sessionUsage` — session token and cost totals, `null` when unknown
- `pending` — steers and queued messages handed to the server and not yet delivered
- `jobs` — background jobs (commands and agents) running in the session
- `permissions` — tool calls waiting for approval, several when parallel subagents ask at once

Every session load, switch or new draft replaces the whole state with `set_messages`, so nothing from the previous session leaks through. The reducer also runs eagerly into a ref, so request and stream callbacks read the current pending list rather than the last render's.

The rendered list is derived, never stored: `buildRenderMessages()` in `utils/messages.ts` projects `rawMessages` and `toolRuntimeById` into `RenderMessage`s on every change. Initial load, live streaming and reconnect replay all go through this one projection, so a reload renders what the stream showed.

### Rendering

- A turn runs from one real user message to the next and renders as one assistant bubble. The assistant messages of a tool loop merge, and tool results fold into their `tool_use` card.
- Manual compaction renders as a standalone divider. Automatic compaction (`meta.trigger: "auto"`) belongs to its turn and renders inside its bubble.
- Background job notifications (text blocks with `meta.job`, `docs/sessions.md`) render as a job marker in the user bubble's place, so the reply is a bubble of its own. A bash marker reads `Background finished` with the exit code and opens to the command and its output; an agent marker reads `Agent finished` with the outcome and opens to the reply. The projection also writes each job's state into the metadata of the call that started it: `running`, then the exit code or outcome, with the output as `job_output`.
- A background bash card opens to the command and its output: the job's output once it has finished, before that its live log (`GET /api/sessions/{id}/logs/{tool_use_id}`), streamed only while the card is open. Its pill reads `background · running` or `background · exit N`.
- An `agent` call renders as `AgentCard`: `agent  <description>` with a status pill (`running`, `12 tools · 45s · $0.03`, `failed`, `stopped`, `background`, `steered`). It opens to the latest steps while a blocking call runs, then to the reply; a background card opens once its job has replied. `Open transcript` appears once the call has named its child in `metadata.session_id`, and opens the child in the transcript dialog.
- A finished turn whose work includes a tool call or automatic compaction folds into one summary row (`Worked for 23s · 2 edits · …`). A stopped or failed turn leads that row with `Stopped` or `Failed`. The running turn shows its work open; it never remounts when it folds.
- A turn ends interrupted when its last record is a `cancelled` or `error` response, or a tool result or automatic compact marker with nothing after it (the SDK persists nothing for a stop between rounds), unless a steer continued the turn. An `error` event adds no content: it marks the tail assistant with the error, which renders as one plain line at the end of the bubble.
- Streaming markdown runs `remend` on the last block only, to close unfinished syntax; finished text renders as written.

Turn stats (`TurnStats`, shown in the bubble footer and the composer):

- History sums the persisted per-request `usage` and `cost`. Streaming takes the latest cumulative `turn_*` values from `usage` events and sums nothing.
- History duration runs between `meta.created_at` timestamps, skipping partial responses, so a reload matches the streamed value.
- `null` means unknown and is not shown. The Web never resolves model pricing.
- The composer's context percentage comes from `currentContext()`, the latest occupancy after the last compact marker. It turns red at 90% of the compaction threshold from `GET /api/config`, or of the context window when auto-compact is off.

### Scrolling

`MessageList` renders long histories as a tail window and prepends older messages as the reader scrolls up, keeping the viewport in place. It follows new output only while the reader is at the bottom; a button brings them back. When a turn folds, `SettleAnchor` keeps what the reader was looking at in place until the transition ends, and any scroll input releases it.

## Runs and Streaming

`useChat` follows the `docs/api.md` contract: `POST /api/chat` starts a run, and `GET /api/runs/{run_id}/stream` streams its events until `[DONE]`. `api.ts` reads the SSE lines and turns a FastAPI error `detail` into an `ApiError`; requests that branch on a status (`409` attach, `404` already delivered) check it themselves.

- A `409` on send or compact means a run is already active: the client attaches to that run's stream.
- A dropped stream reloads the session with `GET /api/sessions/{id}` and attaches to its `active_run`. A snapshot's `pending_events` replay through the same event routing (`routeRunEvents()`) as the live stream, so permissions, stops and compaction come back the same way. If the reload fails, the turn is marked failed.
- Runs this client did not start, such as a session woken by a finished background job, are announced on `GET /api/events`, held open for the page lifetime. `useChat` holds the app's only subscription and forwards events and reconnects to listeners registered through `subscribeEvents()`, such as the transcript dialog. Every event except an agent run's refreshes the session list. A `run_started` for the shown session syncs it: at once when the client is idle, otherwise once the current stream or start request ends.

`useSessionStream` holds what `useChat` and `useAgentSession` share: the reducer, `applySnapshot()`, which loads a snapshot's history and replays its `pending_events`, and `follow()`, which follows one run's stream and reloads the session through the owner's `reload` when the stream drops. Requests, pending items, and stops stay in the owner.

Async work is guarded by ownership checks rather than locks. Each start request (`POST /api/chat`, `POST .../compact`) takes a token; a newer request or a session switch replaces it, and a response whose token or session is no longer current is abandoned. Streams and session loads carry their own tokens the same way. Every step that resumes after an `await` first checks that it still owns what it is about to change.

Stop:

- With a run streaming, Stop aborts the stream and asks the server to stop the run.
- While the start request is still in flight there is no run yet: the turn stays busy, and the run the response names is stopped as soon as it arrives, including one a `409` attaches to. Compaction follows the same rule.
- A session switch instead abandons the response; the run continues in the background.

A rejected send is a request failure, not a turn outcome: the optimistic turn rolls back and `sendError` shows at the tail until the next action clears it.

### Steer and Queue

While a chat run is active, Enter steers the running turn and `⌘/Ctrl+Enter` queues the message for the next turn. Each item gets a client id, sent as `input_id`, and stays in `pending` until a `user_message` event lists it in `meta.input_ids`. Endpoints are in `docs/api.md`.

- A steer the run rejects (`409`) becomes a queued message. A queue request rejected with `409` keeps the item locally and sends it when the stream ends.
- Any other failure, including a network error, returns the item to the composer and sets `sendError`.
- Whoever takes an item out of `pending` (a stop, a stream end, a delivery, a session switch) owns it from then on, so a late response never queues or sends it again.
- A stream end waits for steer and queue requests still in flight before deciding what is left over.

When a chat stream ends, the pending lists empty in one step:

- A clean `[DONE]`: every leftover item, steers then queue, goes out as one `/api/chat` request.
- `cancelled`, `error`, or a disconnect that cannot be recovered: every item goes back to the composer ahead of the current draft, uploads included. After Stop, items the reloaded history shows as delivered are skipped.
- A disconnect that reloads the session takes the snapshot's `pending` instead. A reloaded item whose message carries attachments is `partial`: it cannot be edited back into the composer, since the server holds the attachments.

### Manual Compaction

`/compact` posts `POST /api/sessions/{id}/compact` and follows the returned `kind: "compact"` run like a chat run, without an optimistic message. Only its `compact` event reaches the reducer, and an `error` sets `compactError`. While it runs, the message list shows a pending divider and nothing can be sent; on completion the session reloads.

## Composer

- `Composer` (Lexical) owns the message text and its inline `@path` pills. A submission is `{text, workspaceFiles}`; `useChat` builds the request's `input` blocks from it and the uploads. Pills serialize as `@path` in the text while the file travels as a `path` block, matching the CLI's `@file` behavior.
- `InputArea` owns the uploads (picker, drag, paste) and the draft. Uploads belong to the shown session and clear when it changes; a workspace change remounts `InputArea`, clearing both. Uploads clear only once a submission is accepted, so a rejected one keeps them.
- Restored items come back through `InputArea`'s `prepend` handle, text and uploads together. Image previews come back as data URLs, since their blob URLs were revoked.
- Slash commands complete while idle with no uploads. Skills from `GET /api/config` and `@` workspace files (`GET /api/workspaces/files`) complete while a run is active too. A ref or upload the model cannot ingest blocks submit with a notice; nothing is dropped silently.
- ArrowUp in an empty composer recalls earlier prompts, stored per workspace.
- Esc stops the active run; ⌘K / Ctrl+K opens `SessionSearch`. Both handlers live in `App` and yield to an event already handled, an active IME composition, or an open dialog. An open popover, such as the jobs panel, takes Esc first.
- Running background jobs and queued messages share a card above the composer. Queued messages have actions to steer, edit, or remove.
- While jobs run, the card shows a bar with an icon and count per kind and the longest elapsed time. It opens a panel listing each job with its label, elapsed time, and Stop. A command's row shows that command's live log in the panel; an agent's row opens its transcript.
- `PermissionPrompt` shows `agent: <description>` when a subagent asks.

## Transcript Dialog

`TranscriptDialog` shows a subagent's transcript over the chat. App state `transcript` (`{sessionId, childId}`) chooses it, and App clears it when the shown session changes.

- It is a `ResponsiveDialog`, like `SettingsPanel`: a fixed-size dialog on desktop, a full-screen sheet on mobile. Opening another child swaps the view in place.
- Cards, markers, and the jobs panel reach transcripts and logs through `SessionLinksContext` (`openAgent(childId)`, `logUrl(toolUseId)`) instead of props. App provides the parent session's links; the transcript provides its child's, without `logUrl`, since a subagent runs no background commands. Outside a provider the open actions do not render.
- `useAgentSession` loads `GET /api/sessions/{id}/agents/{child_id}` and follows its `active_run` through `useSessionStream`, then reloads when the run ends so the header shows the outcome. It loads again on a `run_started` for `<parent>/<child>` and on an events reconnect, so a turn that starts after the dialog opened is attached. A load error, such as a `404` for a deleted child, shows in the body.
- The body is a read-only `MessageList`. The header shows the description, the definition name, and a status: `running · <elapsed>`, the outcome from `last`, `starting` while the parent's call or job for the child runs but the child's turn has not begun, otherwise `interrupted`. A running background child has a Stop button.
- While the dialog is open, the parent run's pending permission requests, the subagent's and the parent's own, show under its body, because the dialog covers the composer.

## Configuration

The Web UI persists to `localStorage`: the selected provider, model and workspace, reasoning effort per `provider/model`, recent workspaces, the active session per workspace, prompt history, sidebar width, and theme. A stored provider that is no longer available falls back to the server's default provider, and a stored model to the provider's first model.

The desktop sidebar owns its width: it is saved when a drag, arrow key or reset changes it, and the shown width is clamped to 40% of the window without overwriting the saved value. The mobile drawer has the fixed default width.

The effort selector reads `providers[name].reasoning_efforts[model]` and renders only when the model has values. `SettingsPanel` keeps its form draft inside the dialog content, so each opening starts from the loaded settings and a background refetch does not overwrite unsaved edits. A save updates the SWR cache and revalidates `GET /api/config`.

## Development

```bash
pnpm --dir web install                                 # install dependencies
pnpm --dir web check                                   # oxfmt format check + oxlint (type-aware)
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
