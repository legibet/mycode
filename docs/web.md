# Web UI

React + Vite app in `web/`. The built web assets are copied into `mycode-go/internal/server/webdist/` and embedded into the CLI binary.

## Serving Modes

- `mycode-go web` — serves embedded web assets by default, or `MYCODE_WEB_DIST` when explicitly set
- `mycode-go web --dev` — API only; no static files (pair with `pnpm --dir web dev`)

CORS is enabled for all origins in the HTTP handler.

## Component Structure

```text
web/src/
  App.tsx                # root layout, config loading, session init
  main.tsx               # React entry
  types.ts               # shared TypeScript types
  index.css              # global styles
  components/
    Chat/
      MessageList.tsx      # scrollable message history
      MessageBubble.tsx    # single message, role-based styling
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
    ThemeProvider.tsx      # theme management
    UI/                    # shared UI primitives
  hooks/
    useChat.ts             # main chat state + SSE streaming
    sessionSelection.ts    # session picker state
    *.test.ts(x)           # focused unit and hook tests
  test/
    setup.ts               # Vitest + Testing Library setup
  utils/
    messages.ts            # buildRenderMessages() + streaming message builders
    highlighter.ts         # code syntax highlighting
    storage.ts             # localStorage helpers
    config.ts              # reasoning effort defaults + provider normalization with remote config
    clipboard.ts           # clipboard copy helper
    cn.ts                  # CSS class merging
```

## Message State Model

`useChat.ts` stores three related pieces of state:

- `rawMessages` — canonical block messages
- `messages` — render-ready messages
- `toolRuntimeById` — ephemeral tool runtime state

State is managed via `useReducer` with actions:

- `set_messages` — load session history from server
- `start_turn` — optimistic user message + empty assistant
- `rewind_and_start_turn` — rewind + optimistic new turn
- `apply_event` — apply one SSE event to state

`buildRenderMessages()` in `utils/messages.ts` is used when loading or rebuilding from canonical messages. During streaming, the reducer updates both `rawMessages` and `messages` incrementally.

Key design decisions:

- Tool results persisted as `user` messages with `tool_result` blocks are visually folded into the preceding assistant message during rendering
- Each render message and block gets a stable `renderKey` for React reconciliation
- `sourceIndex` tracks the original message position for scroll targeting

Rendering rules:

- `thinking` blocks → `ReasoningBlock`
- `tool_use` blocks → `ToolCard`
- `text` blocks → `MarkdownBlock`
- `image` blocks → inline image preview in `MessageBubble`

## Streaming

1. `POST /api/chat` → get `{run, session}`
2. `GET /api/runs/{run_id}/stream` → SSE reader
3. Each `data:` line is parsed as one stream event and dispatched to the reducer
4. `data: [DONE]` ends the stream
5. On disconnect, the client reloads session state from `GET /api/sessions/{id}`
6. `409` conflict attaches to the existing run stream

Streaming state tracking:

- `streamTokenRef` — incremented to invalidate stale streams
- `pendingRequestTokenRef` — deduplicates concurrent send requests
- `activeRunRef` — tracks the current run for cancel

Attachments:

- `InputArea` always shows the attachment button and supports file picker and drag-and-drop
- UTF-8 text, code, and config files are attached as the same text snapshot format used by the original CLI `@file`
- Images and PDFs are sent as structured `input` blocks
- The attachment button uses `image_input_models` and `pdf_input_models`; unsupported pending attachments are cleared on model switch

## Config Persistence

Web UI config is persisted to `localStorage`:

- `provider`
- `model`
- `cwd`
- `reasoningEffort`

`auto` and empty string both mean "do not send `reasoning_effort` to the server".

The reasoning effort selector in the sidebar only renders when `supports_reasoning_effort` is true and the current model appears in `reasoning_models` from `GET /api/config`.

## Build

```bash
pnpm --dir web test:run
pnpm --dir web dev
pnpm --dir web build
```

Run `make web-build` to build `web/dist` and sync it into `mycode-go/internal/server/webdist/`.

The server prefers `MYCODE_WEB_DIST` when set. Otherwise it serves the embedded assets compiled into the binary.
