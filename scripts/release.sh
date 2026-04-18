#!/usr/bin/env bash
#
# Release both workspace packages (mycode-sdk and mycode-cli) at the same
# version. Both wheels build, get tagged, and push together.

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

# Bump version in both packages and keep the dependency pin in lock-step.
( cd mycode && uv version "$version" )
( cd cli    && uv version "$version" )
# Refresh the inter-package version pin in cli/pyproject.toml.
uv run --no-project python - "$version" <<'PY'
import re, sys
path = "cli/pyproject.toml"
text = open(path).read()
text = re.sub(r'mycode-sdk==[^"]+', f'mycode-sdk=={sys.argv[1]}', text)
open(path, "w").write(text)
PY
uv lock

rm -rf dist
uv build --package mycode-sdk --no-sources
uv build --package mycode-cli --no-sources

sdk_wheel="$(echo dist/mycode_sdk-*.whl)"
cli_wheel="$(echo dist/mycode_cli-*.whl)"
uv run --isolated --no-project \
  --with "$sdk_wheel" \
  --with "$cli_wheel" \
  -- python -c "import mycode, mycode_cli"

git add mycode/pyproject.toml cli/pyproject.toml uv.lock
git commit -m "Release $version"
git tag -a "$tag" -m "Release $tag"
git push origin "$branch" --follow-tags
