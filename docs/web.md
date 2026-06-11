# Web UI

React + Vite app in `web/`. The same source is used by the Go HTTP server and the Wails desktop app. Built assets are copied to `internal/server/webdist/` for Go embedding.

## Serving Modes

- `mycode-go web` — serves packaged web assets from the embedded `webdist` filesystem, or from `MYCODE_WEB_DIST` / local `web/dist` during development.
- `mycode-go web --dev` — API only; no static files (pair with `pnpm --dir web dev`).
- Wails desktop mode uses the same React source through `web/src/utils/transport.ts`; desktop details live in `docs/wails.md`.

CORS is disabled by default for the packaged web app. The API-only dev handler allows only `http://localhost:5173` and `http://127.0.0.1:5173` for the Vite dev server.

## Component Structure

```text
web/src/
  App.tsx                # root layout, config loading, session init
  main.tsx               # React entry
  types.ts               # shared TypeScript types
  index.css              # Tailwind CSS
  components/
    Chat/
      MessageList.tsx      # scrollable message history
      MessageBubble.tsx    # single message, role-based styling
      CompactMarker.tsx    # inline divider rendered for compact markers
      InputArea.tsx        # user input, file attachments, submit
      ToolCard.tsx         # tool execution block
      ReasoningBlock.tsx   # thinking block
      MarkdownBlock.tsx    # markdown rendering
      CodeBlock.tsx        # syntax-highlighted code
      HighlightedCode.tsx  # shared highlighting wrapper
      EditDiff.tsx         # diff view for edit tool results
      PermissionPrompt.tsx # tool permission approval UI
    Settings/
      SettingsPanel.tsx
      ProviderCard.tsx
      controls.tsx
    Layout.tsx
    Sidebar.tsx
    WorkspacePicker.tsx
    MobileHeader.tsx
    ThemeProvider.tsx
  hooks/
    useChat.ts             # main chat state and streaming
    sessionSelection.ts    # session picker state
  utils/
    transport.ts           # HTTP/Wails transport abstraction
    messages.ts            # block helpers and render projection
    storage.ts             # localStorage helpers
    config.ts              # config defaults and normalization
    highlighter.ts         # code highlighting
```

## Message State Model

`useChat.ts` keeps two pieces of reducer state:

- `rawMessages: ChatMessage[]` - canonical block messages matching the session
  timeline; includes `role: "compact"` markers.
- `toolRuntimeById` - ephemeral tool runtime state for streaming output,
  pending flags, final results, and metadata.

The render-ready list `messages: RenderMessage[]` is derived with
`useMemo(buildRenderMessages(rawMessages, toolRuntimeById))`. There is no
second copy of render-message state to keep synchronized.

`CompactMarkerMessage` (`{kind: "compact-marker", sourceIndex, renderKey}`)
does not carry content. It tells `MessageList` to render `CompactMarker`
instead of `MessageBubble`.

State is managed via `useReducer` with actions:

- `set_messages` - load session history from server
- `start_turn` - optimistic user message plus empty assistant
- `rewind_and_start_turn` - rewind plus optimistic new turn
- `apply_event` - apply one stream event to `rawMessages` or `toolRuntimeById`
- `rollback` - restore the snapshot taken before an optimistic turn

`buildRenderMessages()` is the single projection used by initial load and live
streaming. Tool results visually attach to their `tool_use`; multiple assistant
turns in a tool loop merge into one bubble; every `role: "compact"` entry
renders as a `CompactMarkerMessage`. A live `compact` stream event appends a
canonical compact message, and the marker appears on the next render.

Rendering rules:

- `thinking` blocks render as `ReasoningBlock`.
- `tool_use` blocks render as `ToolCard`.
- Persisted `tool_result` user messages are folded into the preceding assistant
  message.
- `text` blocks render through markdown.
- `image` blocks render inline.
- `compact-marker` entries render as the inline compact divider.

## Transport

Feature code should call `transport` instead of calling `fetch` directly for
backend APIs that also exist in desktop mode.

Browser mode maps transport calls to `/api/...` routes. Wails mode maps the same
calls to Go bindings and runtime events. Shared rendering code receives the same
normalized data and stream events in both modes.

Current transport surface:

- config and settings
- sessions
- chat start, cancel, and tool approval
- workspace browsing
- run events
- desktop commands and window title in Wails mode

`MessageList` renders long histories as a tail window: initial session load renders the latest messages and scrolls to the bottom before paint. Scrolling near the top prepends older messages in batches and preserves the current viewport by restoring the previous distance from the bottom. Auto-scroll follows incoming message updates only while the user is already near the bottom; local height changes such as expanding tools do not trigger it.

## Streaming

Browser mode:

1. `POST /api/chat` returns `{run, session}`.
2. `GET /api/runs/{run_id}/stream` streams SSE.
3. The reducer applies each event.
4. On disconnect, the client reloads `GET /api/sessions/{id}`.
5. If a run is still active, the client applies `pending_events` and reconnects
   with `after=<last_seq>`.
6. `409` conflict attaches to the existing active run.
7. Tool approval decisions are sent with `POST /api/runs/{run_id}/decide`.

Wails mode follows the same reducer path. Go emits normalized run events through
`mycode:run_event`; the hook applies `payload.event` with the same handler used
for SSE. See `docs/wails.md` for the binding and event details.

`permission_request` opens the approval prompt. `permission_resolved` clears it. `deny` cancels the active run.

Streaming state tracking:

- `streamTokenRef` invalidates stale streams.
- `pendingRequestTokenRef` deduplicates concurrent send requests.
- `activeRunRef` tracks the current run for cancel and Wails event matching.

## Attachments

- `InputArea` supports file picker, paste, and drag-and-drop.
- UTF-8 text/code/config files are attached as text snapshots.
- Images and PDFs are sent as structured `input` blocks.
- The attachment button uses `image_input_models` and `pdf_input_models`;
  unsupported pending attachments are cleared on model switch.

## Config Persistence

The web UI persists these local preferences:

- `provider`, `model`, `cwd`, `reasoningEffort`
- active session per cwd
- cwd history
- `auto` and empty string both mean "do not send reasoning_effort to server"
- The reasoning effort selector in the sidebar only renders when `supports_reasoning_effort` is true AND the current model appears in `reasoning_models` (from `GET /api/config`)
- Settings editor options come from `provider_type_env_vars` and `provider_type_default_models`

The settings panel reads and writes global config through
`transport.getSettings()` and `transport.updateSettings()`.

## Build

```bash
pnpm --dir web check
pnpm --dir web typecheck
pnpm --dir web test:run
pnpm --dir web dev
pnpm --dir web build
./scripts/sync_web_dist.sh
```

`./scripts/sync_web_dist.sh` copies `web/dist/` into `internal/server/webdist/`. The Go build tag `embedweb` embeds that directory for release builds. Without embedded assets, the server can still serve an explicit `MYCODE_WEB_DIST` or local `web/dist` directory.

The Go server should work with the same `web/` source as Python `main`.
Backend differences should be handled in Go API compatibility or
`web/src/utils/transport.ts`, not by forking feature components.
