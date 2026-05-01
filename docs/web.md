# Web UI

React + Vite app in `web/`. Built assets are copied to `cli/src/mycode_cli/server/static/` for packaged serving.

## Serving Modes

- `mycode web` — serves packaged web assets from `cli/src/mycode_cli/server/static/`
- `mycode web --dev` — API only with backend hot reload; no static files (pair with `pnpm --dir web dev`)

CORS is enabled for all origins in the FastAPI app.

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
      CompactMarker.tsx    # inline divider rendered for `compact` markers
      InputArea.tsx        # user input, image attachment, submit
      ToolCard.tsx         # tool execution block (start/output/done)
      ReasoningBlock.tsx   # thinking block — expanded while streaming, collapses after
      MarkdownBlock.tsx    # markdown rendering
      CodeBlock.tsx        # syntax-highlighted code
      HighlightedCode.tsx  # shared highlighting wrapper
      EditDiff.tsx         # diff view for edit tool results
    Layout.tsx             # main layout shell
    Sidebar.tsx            # session list + settings panel
    WorkspacePicker.tsx    # workspace browser using /api/workspaces
    MobileHeader.tsx       # mobile nav header
    ThemeProvider.tsx       # light/dark theme toggle
    UI/                    # shared UI primitives
  hooks/
    useChat.ts             # main chat state + SSE streaming
    sessionSelection.ts    # session picker state
    *.test.ts(x)           # focused unit and hook tests
  test/
    setup.ts               # Vitest + Testing Library setup
  utils/
    messages.ts            # buildRenderMessages() + streaming message builders
    highlighter.ts         # code syntax highlighting (shiki)
    storage.ts             # localStorage helpers
    config.ts              # reasoning effort defaults + provider normalization with remote config
    clipboard.ts           # clipboard copy helper
    cn.ts                  # CSS class merging (clsx + tailwind-merge)
```

## Message State Model

`useChat.ts` stores three related pieces of state:

- `rawMessages: ChatMessage[]` — canonical block messages (mirrors the JSONL timeline; includes `role: "compact"` markers)
- `messages: RenderMessage[]` — render-ready entries; `RenderMessage = ChatMessage | CompactMarkerMessage`
- `toolRuntimeById` — ephemeral tool runtime state

`CompactMarkerMessage` (`{kind: "compact-marker", sourceIndex, renderKey}`) carries no content of its own — it just tells `MessageList` to render `CompactMarker` instead of `MessageBubble`. Use the `isCompactMarker(msg)` type guard from `types.ts` to narrow when iterating.

State is managed via `useReducer` with actions:

- `set_messages` — load session history from server
- `start_turn` — optimistic user message + empty assistant
- `rewind_and_start_turn` — rewind + optimistic new turn
- `apply_event` — apply one SSE event to state
- `rollback` — restore the snapshot taken before an optimistic turn

`buildRenderMessages()` in `utils/messages.ts` is used when loading or rebuilding from canonical messages; it emits a `CompactMarkerMessage` for every `role: "compact"` entry it sees. During streaming the reducer updates both `rawMessages` and `messages` incrementally, and a live `compact` SSE event appends a `{role: "compact"}` entry to `rawMessages` plus the matching marker to `messages`.

Key design decisions:

- Tool results persisted as `user` messages with `tool_result` blocks are visually folded into the preceding assistant message during rendering
- Each render message and block gets a stable `renderKey` for React reconciliation
- `sourceIndex` tracks the original message position; rewind uses this index against the visible list, so rewinding to a real user message before a `compact` marker slices the marker away too

Rendering rules:

- `thinking` blocks → `ReasoningBlock` (expanded while streaming, uses `meta.duration_ms` when present)
- `tool_use` blocks → `ToolCard` (with matching `tool_result` and live runtime folded in)
- `text` blocks → `MarkdownBlock`
- `image` blocks → inline image preview in `MessageBubble`
- `compact-marker` entries → `CompactMarker` (a thin labelled divider, no interactivity)

## Streaming

1. `POST /api/chat` → get `{run, session}`
2. `GET /api/runs/{run_id}/stream` → SSE reader
3. Each `data:` line parsed as `StreamEvent`, dispatched to reducer
4. `data: [DONE]` ends the stream
5. On disconnect: attempt session reload recovery via `GET /api/sessions/{id}`
6. 409 conflict: attach to the existing run's stream

A live `compact` SSE event is consumed by the reducer at the position it arrives — the marker lands between whatever just streamed and whatever streams next, mirroring where the agent emitted it (e.g. between two tool calls of the same turn). The server has already persisted the `compact` JSONL record at the same point, so a later session reload renders the same marker without any extra round-trip.

Streaming state tracking:

- `streamTokenRef` — incremented to invalidate stale streams
- `pendingRequestTokenRef` — deduplicates concurrent send requests
- `activeRunRef` — tracks the current run for cancel

Attachments:

- `InputArea` always shows the attachment button and supports file picker and drag-and-drop
- UTF-8 text/code/config files are attached as the same text snapshot format used by CLI `@file`
- Images and PDFs are sent as structured `input` blocks
- The attachment button uses `image_input_models` and `pdf_input_models`; unsupported pending attachments are cleared on model switch

## Config Persistence

Web UI config is persisted to `localStorage`:

- `provider`, `model`, `cwd`, `reasoningEffort`
- `auto` and empty string both mean "do not send reasoning_effort to server"
- The reasoning effort selector in the sidebar only renders when `supports_reasoning_effort` is true AND the current model appears in `reasoning_models` (from `GET /api/config`)

## Build

```bash
pnpm --dir web test:run                                # run web UI tests once
pnpm --dir web dev                                     # dev server (Vite HMR)
uv build --package mycode-cli                          # builds web assets and packages static/ into wheel/sdist
```

Built `web/dist/` is **not** the serving path. `cli/hatch_build.py` copies the built output into `cli/src/mycode_cli/server/static/` during `mycode-cli` package builds, which is what gets packaged and served.

If `cli/src/mycode_cli/server/static/` is missing at startup, the server falls back to API-only mode with a warning log.
