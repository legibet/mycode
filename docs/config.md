# Configuration

Source: `cli/src/mycode_cli/config.py`

## Config Files

Loaded in order (later values override earlier):

1. `~/.mycode/config.json` — global
2. `.mycode/config.json` files from `project` to `cwd`

`project` is the nearest parent directory containing `.git`. When no `.git` is found, `project` is `cwd`.

Explicit request args (CLI flags, API params) override both.

Config resolution: `get_settings(cwd)` → returns `Settings` dataclass.

The web UI's settings panel edits **only the global file**; project-level files
must be edited by hand and continue to override it. See `GET /api/settings` and
`PUT /api/settings` in `docs/api.md`.

## Schema

```json
{
  "default": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "reasoning_effort": "auto",
    "compact_threshold": 0.8
  },
  "permission": {
    "level": "safe",
    "mode": "ask"
  },
  "providers": {
    "<name>": {
      "type": "<adapter-id>",
      "models": {
        "model-a": {
          "context_window": 400000,
          "max_output_tokens": 128000,
          "supports_reasoning": true,
          "supports_image_input": true,
          "supports_pdf_input": true
        },
        "model-b": {}
      },
      "base_url": "https://...",
      "api_key": "sk-..." or "${ENV_VAR_NAME}",
      "reasoning_effort": "none"
    }
  }
}
```

### Fields

- `default.provider` — references a key in `providers`, or a raw adapter id
- `default.model` — model name used when no per-provider model is set
- `default.reasoning_effort` — global default; `null`/`"auto"`/`"default"` all resolve to "no override"
- `default.compact_threshold` — fraction of context window that triggers compaction; `false` or `0` disables; range `[0, 1]`; default `0.8`
- `permission` — CLI tool execution permissions. String shorthand (`"safe"`) sets the level and keeps the current/default mode; object form accepts `level` and `mode`
- `permission.level` — how much the agent may run automatically: `readonly` · `safe` · `standard` · `yolo`; default `safe`
- `permission.mode` — what to do outside the selected level: `ask` or `deny`; default `ask`. Non-interactive `mycode run` treats `ask` as `deny`
- `providers.<name>.type` — internal adapter id (see AGENTS.md provider table). Required for custom aliases. Built-in providers can omit `type` when the key matches their adapter id.
- `providers.<name>.models` — model map. Keys are model ids shown in UI. Values can override the bundled model metadata for that exact model.
- `providers.<name>.models.<model>.context_window` — override the model context window
- `providers.<name>.models.<model>.max_output_tokens` — override the provider output limit
- `providers.<name>.models.<model>.supports_reasoning` — override whether reasoning effort is available
- `providers.<name>.models.<model>.supports_image_input` — override image input support
- `providers.<name>.models.<model>.supports_pdf_input` — override PDF input support
- `providers.<name>.api_key` — literal value or `${ENV_NAME}` reference
- `providers.<name>.base_url` — override the adapter's default base URL
- `providers.<name>.reasoning_effort` — per-provider override of the global default

## API Key Resolution Order

For a resolved provider (`_resolve_provider_runtime` in `config.py`):

1. Explicit `api_key` param (CLI flag or API request)
2. Config `api_key`
   - `${ENV_NAME}` — dereferenced from env at resolution time
   - plain string — used as-is
3. Provider adapter's built-in default env vars (e.g., `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)

If no API key is found at any step, provider resolution raises an error listing which env vars were checked.

## Provider Resolution

`resolve_provider(settings, provider_name=..., model=...)` returns a `ResolvedProvider`:

1. If `provider_name` given: resolve it as a configured alias or raw provider id
2. If no provider given: try the configured default
3. Fallback: iterate configured providers with valid credentials, then env-discoverable built-in providers
4. If nothing found: raise error listing checked env vars

Auto-discovery is limited to providers where `auto_discoverable=True` and the corresponding env var is set.

## Reasoning Effort

Controls how much thinking a model does.

Config resolution: `providers.<name>.reasoning_effort` → `default.reasoning_effort`

Request override: `POST /api/chat` normalizes `reasoning_effort` and passes it through directly when set.

Options: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`

- `auto` — do not send any effort parameter; let the provider decide
- `none` — explicitly disable thinking
- Config-derived effort is applied only when `adapter.supports_reasoning_effort` AND `model_metadata.supports_reasoning` (from the bundled catalog) are both true
- CLI `/effort` command and web sidebar allow per-request overrides without changing config
- See `docs/providers.md` for per-adapter mapping details

## Tool Permissions

CLI and web server agents use SDK tool hooks to classify tool calls before execution. Both interactive surfaces (TUI and web) prompt the user for approval when `mode: "ask"` and the call falls outside the configured `level`. Non-interactive `mycode run` has no prompt, so it treats `ask` as `deny` and returns the denial to the model as the tool result.

Levels:

- `readonly` — automatically allow clear read-only actions under `project`, discovered skill reads, and simple read-only shell commands (`ls`, `rg`, `git status`, `git diff`, etc.)
- `safe` — `readonly` plus `project`-local `write`/`edit`. Shell commands remain limited to clear read-only commands.
- `standard` — `safe` plus ordinary single shell commands, unless they match dangerous or compound-command checks
- `yolo` — automatically allow all tool calls

Mode:

- `ask` — prompt for approval (TUI inline picker or web prompt panel above the input area)
- `deny` — reject without prompting

Automatic denials do not stop the run; the model receives the denied tool result and can reply with next steps. An explicit user `Deny` cancels the current run in both TUI and web.

The shell checks are intentionally simple and conservative. Project commands such as tests, builds, formatters, package scripts, and task runners are `standard` because they execute project-defined code. Compound commands (`&&`, `||`, `;`, pipes, redirection, command substitution) and obvious destructive commands (`rm`, `sudo`, `chmod`, `git reset`, `git clean`, `git push --force`, etc.) fall outside `readonly`/`safe`/`standard` and require `yolo` or `mode: "ask"` approval.

## Model Metadata

`mycode/src/mycode/models.py` reads the bundled `mycode/src/mycode/models_catalog.json` catalog to look up:

- `supports_reasoning` — whether the model supports extended thinking
- `supports_image_input` — whether the model accepts image input
- `supports_pdf_input` — whether the model accepts PDF input
- `context_window` — used for compact threshold calculation; defaults to `128000` when not available
- `max_output_tokens` — passed to the provider as the output limit; defaults to `16384` when not available

When the catalog has no match and config does not override the capability, media and reasoning support stay disabled: image/PDF input is rejected, and `reasoning_effort` is only sent when `supports_reasoning` is explicitly `true` and the provider adapter supports it.

Model lookup strategy (`lookup_model_metadata`):

1. Exact match on the given `provider_type` + raw model id
2. Fallback provider mapping (e.g., `claude-*` → `anthropic`, `deepseek-*` → `deepseek`)
3. OpenRouter catalog suffix fallback (`provider/model` matched by `model`) as last resort

The bundled catalog is updated by running:

```bash
uv run python scripts/update_models_catalog.py
```

## Skills Discovery

`cli/src/mycode_cli/system_prompt.py` scans for `SKILL.md` files and injects an `<available_skills>` block into the system prompt.

Scan roots (lowest to highest priority):

1. `~/.agents/skills/` — compatibility global root
2. `~/.mycode/skills/` — global root
3. `.agents/skills/` from `project` to `cwd` — compatibility project roots
4. `.mycode/skills/` from `project` to `cwd` — project roots

Each `SKILL.md` requires YAML frontmatter with `name` and `description`. Later roots override earlier ones by skill name. Max scan depth: 3 directory levels, max 200 directories per root.

The model uses the `read` tool to load full skill content on demand from the skill `path`.

## Instructions Discovery

`cli/src/mycode_cli/system_prompt.py` reads `AGENTS.md` files and injects them as `<project_instructions>` into the system prompt. Files checked:

1. `~/.mycode/AGENTS.md` (fallback: `~/.agents/AGENTS.md`)
2. all `AGENTS.md` files from `project` to `cwd`

Later files are more specific and take precedence.

## Project Boundary

`cwd` is the current working directory. `project` is the nearest parent directory containing `.git`; when no `.git` is found, `project` is `cwd`.

Config, instructions, and skill discovery walk from `project` to `cwd`, so nearer files have higher priority. Tool permissions treat paths inside `project` as project-local and require approval for paths outside `project`.

## Sessions Directory

`resolve_sessions_dir()` → `~/.mycode/sessions/` (or `$MYCODE_HOME/sessions/`). See `docs/sessions.md`.

## Port

Server port: `PORT` env var → `settings.port` (default `8000`). Overridden by `--port` CLI flag.
