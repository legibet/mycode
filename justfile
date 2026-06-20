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
  git submodule update --init --recursive
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
    if [ -n "${backend_pid:-}" ]; then
      kill "$backend_pid" 2>/dev/null || true
    fi
    if [ -n "${frontend_pid:-}" ]; then
      kill "$frontend_pid" 2>/dev/null || true
    fi
  }

  stop() {
    exit 0
  }

  trap cleanup EXIT
  trap stop INT TERM

  echo "Starting backend on http://localhost:8000"
  uv run mycode web --dev &
  backend_pid=$!

  echo "Starting frontend on http://localhost:5173"
  pnpm --dir web dev &
  frontend_pid=$!

  exit_code=0
  while :; do
    if ! kill -0 "$backend_pid" 2>/dev/null; then
      wait "$backend_pid" || exit_code=$?
      break
    fi
    if ! kill -0 "$frontend_pid" 2>/dev/null; then
      wait "$frontend_pid" || exit_code=$?
      break
    fi
    sleep 1
  done

  exit "$exit_code"

py-check:
  uv run ruff check .
  uv run basedpyright

check: py-check

py-test:
  uv run pytest

test: py-test

fmt:
  uv run ruff check --fix .
  uv run ruff format .

build:
  uv build --package mycode-sdk
  uv build --package mycode-cli
