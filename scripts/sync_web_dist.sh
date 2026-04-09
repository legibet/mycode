#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WEB_DIST_DIR="$ROOT_DIR/web/dist"
TARGET_DIR="$ROOT_DIR/mycode-go/internal/server/webdist"

if [ ! -d "$WEB_DIST_DIR" ]; then
  echo "web build output not found: $WEB_DIST_DIR" >&2
  exit 1
fi

rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp -R "$WEB_DIST_DIR"/. "$TARGET_DIR"/
