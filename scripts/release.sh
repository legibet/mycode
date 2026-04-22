#!/usr/bin/env bash
#
# Release mycode-sdk and mycode-cli at the same version.

set -euo pipefail
shopt -s nullglob

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <version>" >&2
  exit 1
fi

version="$1"
tag="v$version"
branch="$(git branch --show-current)"

if [[ "$version" == v* ]]; then
  echo "Pass the package version without a leading 'v'." >&2
  exit 1
fi

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

# Update package metadata first, then refresh the lockfile once below.
uv version --package mycode-sdk --frozen "$version"
uv version --package mycode-cli --frozen "$version"

# Keep the published CLI dependency aligned with the SDK release.
pin_count="$(grep -Fc '"mycode-sdk==' cli/pyproject.toml || true)"
if [[ "$pin_count" != "1" ]]; then
  echo "Expected one mycode-sdk dependency pin in cli/pyproject.toml, found $pin_count." >&2
  exit 1
fi

MYCODE_RELEASE_VERSION="$version" perl -0pi -e \
  's/mycode-sdk==[^"]+/mycode-sdk==$ENV{MYCODE_RELEASE_VERSION}/' \
  cli/pyproject.toml

pin_count="$(grep -Fc "\"mycode-sdk==$version\"," cli/pyproject.toml || true)"
if [[ "$pin_count" != "1" ]]; then
  echo "Failed to update the mycode-sdk dependency pin." >&2
  exit 1
fi

uv lock

rm -rf dist
# Build publishable artifacts without workspace-only sources.
uv build --package mycode-sdk --no-sources
uv build --package mycode-cli --no-sources

sdk_wheels=(dist/mycode_sdk-*.whl)
cli_wheels=(dist/mycode_cli-*.whl)

if [[ ${#sdk_wheels[@]} -ne 1 ]]; then
  echo "Expected one mycode-sdk wheel in dist, found ${#sdk_wheels[@]}." >&2
  exit 1
fi

if [[ ${#cli_wheels[@]} -ne 1 ]]; then
  echo "Expected one mycode-cli wheel in dist, found ${#cli_wheels[@]}." >&2
  exit 1
fi

sdk_wheel="${sdk_wheels[0]}"
cli_wheel="${cli_wheels[0]}"

# Verify the wheels, not the local source tree.
sdk_metadata_version="$(
  uv run --isolated --no-project --with "$sdk_wheel" -- \
    python -c 'from importlib import metadata; print(metadata.version("mycode-sdk"))'
)"
sdk_runtime_version="$(
  uv run --isolated --no-project --with "$sdk_wheel" -- \
    python -c 'import mycode; print(mycode.__version__)'
)"
cli_metadata_version="$(
  uv run --isolated --no-project --with "$sdk_wheel" --with "$cli_wheel" -- \
    python -c 'from importlib import metadata; print(metadata.version("mycode-cli"))'
)"
cli_runtime_version="$(
  uv run --isolated --no-project --with "$sdk_wheel" --with "$cli_wheel" -- \
    python -c 'import mycode_cli; print(mycode_cli.__version__)'
)"
cli_command_version="$(
  uv run --isolated --no-project --with "$sdk_wheel" --with "$cli_wheel" -- \
    mycode --version
)"

if [[ "$sdk_metadata_version" != "$version" || "$sdk_runtime_version" != "$version" ]]; then
  echo "SDK wheel version mismatch: metadata=$sdk_metadata_version runtime=$sdk_runtime_version expected=$version" >&2
  exit 1
fi

if [[ "$cli_metadata_version" != "$version" || "$cli_runtime_version" != "$version" ]]; then
  echo "CLI wheel version mismatch: metadata=$cli_metadata_version runtime=$cli_runtime_version expected=$version" >&2
  exit 1
fi

if [[ "$cli_command_version" != "mycode $version" ]]; then
  echo "mycode --version returned '$cli_command_version', expected 'mycode $version'" >&2
  exit 1
fi

git add mycode/pyproject.toml cli/pyproject.toml uv.lock
if ! git diff --cached --quiet; then
  git commit -m "Release $version"
fi

git tag -a "$tag" -m "Release $tag"
git push origin "$branch" --follow-tags
