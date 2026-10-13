set shell := ["bash", "-uc"]
set dotenv-load

alias help := default
alias install := setup
alias dev := web-dev
alias api := web-backend
alias backend := web-backend
alias ui := web-frontend
alias frontend := web-frontend
alias web := web-dev

default:
  @just --list

setup:
  uv sync --dev
  pnpm --dir web install

chat:
  uv run mycode

web-backend:
  uv run mycode web --dev

web-frontend:
  pnpm --dir web dev

web-dev:
  #!/usr/bin/env bash
  set -euo pipefail

  cleanup() {
    kill "${backend_pid:-}" "${frontend_pid:-}" 2>/dev/null || true
    wait
  }
  trap cleanup EXIT
  trap 'exit 0' INT TERM

  uv run mycode web --dev &
  backend_pid=$!
  pnpm --dir web dev &
  frontend_pid=$!

  wait -n

py-check:
  uv run ruff check .
  uv run ruff format --check .
  uv run ty check

web-check:
  pnpm --dir web check
  pnpm --dir web typecheck

check: py-check web-check

py-test:
  uv run pytest

web-test:
  pnpm --dir web test

test: py-test web-test

ci: check test

fmt:
  uv run ruff check --fix .
  uv run ruff format .
  pnpm --dir web check:fix

build:
  uv build --package mycode-sdk
  uv build --package mycode-cli
