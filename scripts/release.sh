#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <version>" >&2
  exit 1
fi

version="$1"
tag="v$version"
branch="$(git branch --show-current)"

if [[ -z "$branch" ]]; then
  echo "Current branch is detached; release from a branch." >&2
  exit 1
fi

if [[ -n "$(git status --short)" ]]; then
  echo "Working tree is not clean." >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "Tag $tag already exists." >&2
  exit 1
fi

uv version "$version"
uv lock

git add pyproject.toml uv.lock
git commit -m "Release $version"
git tag -a "$tag" -m "Release $tag"
git push origin "$branch" --follow-tags
