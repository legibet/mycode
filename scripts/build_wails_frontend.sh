#!/usr/bin/env sh
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TARGET_DIR="$REPO_DIR/frontend/dist"

pnpm --dir "$REPO_DIR/web" typecheck
pnpm --dir "$REPO_DIR/web" build

# Clear previous build artifacts but keep the tracked .gitkeep, which
# is required so //go:embed all:frontend/dist compiles on fresh clones.
mkdir -p "$TARGET_DIR"
find "$TARGET_DIR" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +
cp -R "$REPO_DIR/web/dist/." "$TARGET_DIR/"
