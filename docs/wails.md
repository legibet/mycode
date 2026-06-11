# Wails Desktop App

The Wails desktop target lives on the **`mycode-go-wails`** branch only. The
branch model is recorded in `AGENTS.md`; this file only covers desktop structure
and packaging.

## Architecture

The desktop app is a native Wails app, not a browser wrapper around the HTTP
server. The goal is to keep the `mycode-go` backend and shared `web/` UI intact,
then add one thin native transport layer on this branch.

- `main.go` starts Wails and embeds `frontend/dist`.
- `app.go` exposes Go methods through Wails bindings.
- `internal/core/` owns shared chat, session, run, config, and workspace logic.
- `internal/server/` is only the HTTP adapter for browser mode.
- `web/src/utils/transport.ts` selects Wails bindings when `window.go.main.App` exists, otherwise it uses the HTTP API.

Rules:

- Keep `internal/core/` transport-agnostic.
- Keep `internal/server/` HTTP-only.
- Keep feature components unaware of HTTP vs Wails.
- Put native desktop behavior in `app.go`, `main.go`, or `transport.ts`.
- Route every new browser API call through `transport.ts` before syncing it here.
- Desktop chrome behavior belongs in the Wails layer. External links open with
  the system browser, native menu commands emit runtime events, the window title
  follows the active session, and the hidden macOS titlebar uses web chrome at
  the top of the window.

The Wails bindings return an `APIResult` envelope. `transport.ts` unwraps it so
feature components see the same data and error behavior as browser mode. Current
bindings:

- `GetConfig`, `Settings`, `UpdateSettings`
- `ListSessions`, `LoadSession`, `DeleteSession`, `ClearSession`
- `StartChat`, `CancelRun`, `DecideRun`
- `SelectFiles`
- `WorkspaceRoots`, `BrowseWorkspace`

`SelectFiles` is the desktop-only file picker bridge used by attachment inputs.
Settings, run cancellation, and tool permission decisions go through bindings in
Wails mode and the matching `/api/...` routes in browser mode.

Live run events use the Wails runtime event `mycode:run_event`.

Payload:

```json
{
  "run_id": "...",
  "session_id": "...",
  "event": {
    "type": "text",
    "seq": 1,
    "delta": "..."
  }
}
```

The inner `event` uses the same normalized event names as the HTTP SSE contract.
The React hook applies SSE events and Wails runtime events through the same event
handler, so `permission_request`, `permission_resolved`, `compact`, tool output,
and text deltas behave the same in both modes. Wails additionally emits
`{"type":"done"}` when a run finishes.

Native menu commands use the Wails runtime event `mycode:desktop_command`.
`transport.ts` maps those commands to existing UI actions so feature components
do not call Wails runtime APIs directly.

Keep Wails-specific UI tweaks narrow:

- no minimum window size for layout fixes
- no separate narrow-window layout to maintain
- top safe spacing is applied only when `data-mycode-desktop="wails"`
- shared feature components should continue to work in browser mode

## Development

Install Wails CLI when needed:

```bash
make wails-install
make wails-doctor
```

Run the desktop app in development mode:

```bash
make wails-dev
```

Build the desktop app (recommended one-shot wrapper, runs
`pnpm install --frozen-lockfile`, `GOWORK=off wails build -clean`, and an
ad-hoc `codesign` so double-click works without Gatekeeper warnings):

```bash
make wails-build
```

Pass extra Wails build flags when needed:

```bash
make wails-build WAILS_FLAGS="-debug"
```

The macOS app is written to:

```text
build/bin/mycode.app
```

## Frontend Assets

Wails embeds assets from `frontend/dist`, so the build script copies the shared web build there before packaging.

```bash
make wails-frontend
```

Generated desktop assets and Wails bindings are ignored:

- `frontend/dist/`
- `wailsjs/`

`GOWORK=off` keeps Wails commands using the root Go module directly.

## Verification

```bash
go test ./...
pnpm --dir web typecheck
pnpm --dir web test:run
make wails-build
git status --short
```
