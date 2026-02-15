# mycode (Personal Minimal Coding Agent)

## Goals

- Keep the core **minimal** and **reliable**.
- Only expose **4 base tools** to the model: `read`, `write`, `edit`, `bash`.
- Everything else should be added later as **skills** (external scripts + docs), not as core tools.

## Language

- UI text can be English.
- Model/system instructions should favor concise output.

## Tooling & Conventions

- Python only. Use **uv** for env/deps.
- Formatting/linting: `ruff`.
- Prefer small modules with clear types and docstrings.

## System Prompt Principles

- Explicitly instruct the model:
  - Use `bash` with `rg` for search.
  - Use `read` before `edit`.
  - Keep tool outputs short; large outputs should be written to files and then read.

## Sessions

- Sessions are append-only.
- Prefer JSONL logs over rewriting full state.
