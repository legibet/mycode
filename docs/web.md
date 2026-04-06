# Web UI

React + Vite app in `web/`. Built assets are copied to `mycode/server/static/` for packaged serving.

## Serving Modes

- `mycode web` — serves packaged web assets from `mycode/server/static/`
- `mycode web --dev` — API only; no static files (pair with `pnpm --dir web dev`)

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

- `thinking` blocks → `ReasoningBlock` (expanded while streaming, auto-collapses after)
- `tool_use` blocks → `ToolCard` (with matching `tool_result` and live runtime folded in)
- `text` blocks → `MarkdownBlock`
- `image` blocks → inline image preview in `MessageBubble`

## Streaming

1. `POST /api/chat` → get `{run, session}`
2. `GET /api/runs/{run_id}/stream` → SSE reader
3. Each `data:` line parsed as `StreamEvent`, dispatched to reducer
4. `data: [DONE]` ends the stream
5. On disconnect: attempt session reload recovery via `GET /api/sessions/{id}`
6. 409 conflict: attach to the existing run's stream

Streaming state tracking:

- `streamTokenRef` — incremented to invalidate stale streams
- `pendingRequestTokenRef` — deduplicates concurrent send requests
- `activeRunRef` — tracks the current run for cancel

Image input:

- `InputArea` supports file picker and drag-and-drop
- Images are sent as structured `input` blocks
- The attachment button uses `image_input_models`; pending images are cleared on unsupported model switch

## Config Persistence

Web UI config is persisted to `localStorage`:

- `provider`, `model`, `cwd`, `reasoningEffort`
- `auto` and empty string both mean "do not send reasoning_effort to server"
- The reasoning effort selector in the sidebar only renders when `supports_reasoning_effort` is true AND the current model appears in `reasoning_models` (from `GET /api/config`)

## Build

```bash
pnpm --dir web test:run                                # run web UI tests once
pnpm --dir web dev                                     # dev server (Vite HMR)
uv run --no-project python scripts/build_web.py       # production build → mycode/server/static/
uv build                                               # packages static/ into wheel/sdist
```

Built `web/dist/` is **not** the serving path. `scripts/build_web.py` copies the built output into `mycode/server/static/` which is what gets packaged and served.

If `mycode/server/static/` is missing at startup, the server falls back to API-only mode with a warning log.
