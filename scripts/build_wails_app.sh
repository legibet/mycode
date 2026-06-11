#!/usr/bin/env sh
# One-shot Wails desktop build for local/personal use.
# Usage: ./scripts/build_wails_app.sh [extra wails build flags]
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WAILS_VERSION="v2.12.0"
WAILS_BIN="$(GOWORK=off go env GOPATH)/bin/wails"

if [ ! -x "$WAILS_BIN" ]; then
  echo "Wails CLI not found at $WAILS_BIN"
  echo "Install with: GOWORK=off go install github.com/wailsapp/wails/v2/cmd/wails@${WAILS_VERSION}"
  exit 1
fi

echo "==> Installing web deps (frozen)"
pnpm --dir "$REPO_DIR/web" install --frozen-lockfile

echo "==> Building Wails app (cleanBuildDirectory)"
cd "$REPO_DIR"
GOWORK=off "$WAILS_BIN" build -clean "$@"

APP_PATH="$REPO_DIR/build/bin/mycode.app"
if [ -d "$APP_PATH" ]; then
  echo "==> Ad-hoc codesign"
  codesign --sign - --deep --force "$APP_PATH" >/dev/null 2>&1 || true
  echo "==> Removing quarantine attrs"
  xattr -cr "$APP_PATH" 2>/dev/null || true
  echo ""
  echo "Built: $APP_PATH"
fi
