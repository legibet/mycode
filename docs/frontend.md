# Frontend

React + Vite app in `frontend/`. Built assets are copied to `mycode/server/static/` for packaged serving.

## Serving Modes

- `mycode web` — serves packaged frontend from `mycode/server/static/`
- `mycode web --dev` — API only; no static files (pair with `pnpm --dir frontend dev`)

## Component Structure

```
frontend/src/
  App.jsx
  components/
    Chat/
      MessageList.jsx      # scrollable message history
      MessageBubble.jsx    # single message, role-based styling
      InputArea.jsx        # user input + submit
      ToolCard.jsx         # tool execution block (start/output/done)
      ReasoningBlock.jsx   # thinking block — expanded while streaming, collapses after
      MarkdownBlock.jsx    # markdown rendering
      CodeBlock.jsx        # syntax-highlighted code
      EditDiff.jsx         # diff view for edit tool results
    Layout.jsx
    Sidebar.jsx            # session list + settings panel
    WorkspacePicker.jsx    # workspace browser using /api/workspaces
    MobileHeader.jsx
    ThemeProvider.jsx
    UI/Button.jsx
    UI/Input.jsx
  hooks/
    useChat.js             # main chat state + SSE streaming
    sessionSelection.js    # session picker state
  utils/
    messages.js            # buildRenderMessages()
    highlighter.js         # code syntax highlighting
    storage.js             # localStorage helpers
    config.js              # reasoning effort defaults + provider normalization with remote config
    clipboard.js
    cn.js                  # CSS class merging
  index.css                # Tailwind CSS
```

## Message State Model

`useChat.js` stores raw canonical blocks plus ephemeral tool runtime state. It does **not** maintain a separate rendered message list.

`buildRenderMessages()` in `utils/messages.js` derives UI messages from canonical blocks on each render. This is the single source of truth for what appears in the UI.

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
