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
continue to override it. Config document validation and runtime resolution live
in `cli/src/mycode_cli/config.py`; the settings API only reads/writes the global
file and adapts it for the UI.

## Schema

```json
{
  "default": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
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
          "reasoning_efforts": ["low", "medium", "high"],
          "supports_image_input": true,
          "supports_pdf_input": true
        },
        "model-b": {}
      },
      "base_url": "https://...",
      "api_key": "sk-..." or "${ENV_VAR_NAME}"
    }
  }
}
```

### Fields

- `default.provider` — references a key in `providers`, or a raw adapter id
- `default.model` — model name used when no per-provider model is set
- `default.compact_threshold` — fraction of context window that triggers compaction; `false` or `0` disables; range `[0, 1]`; default `0.8`
- `permission` — CLI tool execution permissions. String shorthand (`"safe"`) sets the level and keeps the current/default mode; object form accepts `level` and `mode`
- `permission.level` — how much the agent may run automatically: `readonly` · `safe` · `standard` · `yolo`; default `safe`
- `permission.mode` — what to do outside the selected level: `ask` or `deny`; default `ask`. Non-interactive `mycode run` treats `ask` as `deny`
- `providers.<name>.type` — internal adapter id (see AGENTS.md provider table). Required for custom aliases. Built-in providers can omit `type` when the key matches their adapter id.
- `providers.<name>.models` — model map. Keys are model ids shown in UI. Values can override the bundled model metadata for that exact model.
- `providers.<name>.models.<model>.context_window` — override the model context window
- `providers.<name>.models.<model>.max_output_tokens` — override the provider output limit
- `providers.<name>.models.<model>.reasoning_efforts` — override the model's available effort values; an empty list disables effort selection for that model
- `providers.<name>.models.<model>.supports_image_input` — override image input support
- `providers.<name>.models.<model>.supports_pdf_input` — override PDF input support
- `providers.<name>.api_key` — literal value or `${ENV_NAME}` reference
- `providers.<name>.base_url` — override the adapter's default base URL
- `providers.<name>.supports_reasoning_effort` — opt-in (default `false`) for a generic `openai_chat` endpoint that accepts the standard top-level `reasoning_effort`. Ignored for other provider types, which declare effort support in their adapter

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

1. If `provider_name` given: resolve it as a configured alias or raw provider id; failures raise.
2. If no `provider_name`: try the configured default; failures fall through to step 3.
3. Iterate configured providers with valid credentials, then env-discoverable built-in providers.
4. If nothing found: raise error listing checked env vars.

Auto-discovery is limited to providers where `auto_discoverable=True` and the corresponding env var is set.

`ResolvedProvider.model_config` is the selected model's config override, or `None`.

## Reasoning Effort

Controls how much thinking a model does.

Available values come from the selected model's metadata or its `reasoning_efforts` override. TUI and Web prepend `auto`; models without values show no effort control. Without an explicit request or a frontend model-specific preference, effort is `auto`.

- An omitted `POST /api/chat` field sends no effort
- An explicit unsupported request value returns `400`
- CLI `/effort` and the Web input control remember effort per provider/model without changing config
- TUI preferences are stored in `~/.mycode/tui.json`; WebUI preferences stay in browser local storage
- `mycode run --effort <level>` sets effort for one non-interactive run; omitted means `auto`
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

- `reasoning_efforts` — effort values available for the model
- `supports_image_input` — whether the model accepts image input
- `supports_pdf_input` — whether the model accepts PDF input
- `context_window` — used for compact threshold calculation; defaults to `128000` when not available
- `max_output_tokens` — passed to the provider as the output limit; defaults to `16384` when not available

When the catalog has no match and config does not override the capability, media and effort controls stay disabled. Image/PDF input is rejected, and no effort is sent.

Model lookup strategy (`lookup_model_metadata`):

1. Exact match on the given `provider_type` + raw model id
2. Official bare-model fallback generated from the model owner's selected provider

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

Each `SKILL.md` requires YAML frontmatter with `name` and `description`. `name` must match `[a-zA-Z0-9_-]+` and be ≤ 64 chars, or the skill is skipped. A `description` over the Agent Skills spec limit of 1024 chars loads with a logged warning, as does a catalog over 16k chars. Later roots override earlier ones by skill name. Max scan depth: 3 directory levels, max 200 directories per root.

Skill metadata is XML-escaped in the `<available_skills>` block; the `/<skill-name>` snapshot body is not escaped.

A symlinked skill directory contributes its top-level `SKILL.md`. Recursive scanning follows physical directories.

The model uses the `read` tool to load full skill content from the skill `path`.

A standalone `/<skill-name>` token references a discovered skill, for example `Use /fastapi to review this route`. Each matching skill prepends one snapshot containing the frontmatter-free `SKILL.md` body, file location, and base directory. Repeated references share one snapshot. Other slash tokens are sent as text.

Slash completion reserves these built-in command names and their prefixes: `clear`, `compact`, `new`, `resume`, `rewind`, `provider`, `model`, `effort`, and `q`. Skill names outside this set are available for completion.

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
