# Frontend

React + Vite app in `frontend/`. Built assets are copied to `mycode/server/static/` for packaged serving.

## Serving Modes

- `mycode web` — serves packaged frontend from `mycode/server/static/`
- `mycode web --dev` — API only; no static files (pair with `pnpm --dir frontend dev`)

## Component Structure

```
frontend/src/
  App.tsx
  main.tsx
  types.ts
  components/
    Chat/
      MessageList.tsx      # scrollable message history
      MessageBubble.tsx    # single message, role-based styling
      InputArea.tsx        # user input + submit
      ToolCard.tsx         # tool execution block (start/output/done)
      ReasoningBlock.tsx   # thinking block — expanded while streaming, collapses after
      MarkdownBlock.tsx    # markdown rendering
      CodeBlock.tsx        # syntax-highlighted code
      EditDiff.tsx         # diff view for edit tool results
    Layout.tsx
    Sidebar.tsx            # session list + settings panel
    WorkspacePicker.tsx    # workspace browser using /api/workspaces
    MobileHeader.tsx
    ThemeProvider.tsx
    UI/Button.tsx
    UI/Input.tsx
  hooks/
    useChat.ts             # main chat state + SSE streaming
    sessionSelection.ts    # session picker state
  utils/
    messages.ts            # buildRenderMessages()
    highlighter.ts         # code syntax highlighting
    storage.ts             # localStorage helpers
    config.ts              # reasoning effort defaults + provider normalization with remote config
    clipboard.ts
    cn.ts                  # CSS class merging
  index.css                # Tailwind CSS
```

## Message State Model

`useChat.ts` stores raw canonical blocks plus ephemeral tool runtime state. It does **not** maintain a separate rendered message list.

`buildRenderMessages()` in `utils/messages.ts` derives UI messages from canonical blocks on each render. This is the single source of truth for what appears in the UI.

Rendering rules:

- `thinking` blocks → `ReasoningBlock` (expanded while streaming, auto-collapses after)
- `tool_use` blocks → `ToolCard` (with matching `tool_result` and live runtime folded in)
- `text` blocks → `MarkdownBlock`

## Config Persistence

Frontend config is persisted to `localStorage`:

- `provider`, `model`, `cwd`, `reasoningEffort`
- `auto` and empty string both mean "do not send reasoning_effort to server"
- The reasoning effort selector in the sidebar only renders when `supports_reasoning_effort` is true AND the current model appears in `reasoning_models` (from `GET /api/config`)

## Build

```bash
pnpm --dir frontend dev                                # dev server (Vite HMR)
uv run --no-project python scripts/build_frontend.py  # production build → mycode/server/static/
uv build                                               # packages static/ into wheel/sdist
```

Built `frontend/dist/` is **not** the serving path. `scripts/build_frontend.py` copies the built output into `mycode/server/static/` which is what gets packaged and served.
