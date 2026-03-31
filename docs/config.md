# Configuration

Source: `mycode/core/config.py`

## Config Files

Loaded in order (later values override earlier):

1. `~/.mycode/config.json` — global
2. `<workspace>/.mycode/config.json` — project-specific (found by walking up from cwd to `.git` root)

Explicit request args (CLI flags, API params) override both.

Config resolution: `get_settings(cwd)` → returns `Settings` dataclass.

## Schema

```json
{
  "default": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "reasoning_effort": "auto",
    "compact_threshold": 0.8
  },
  "providers": {
    "<name>": {
      "type": "<adapter-id>",
      "models": ["model-a", "model-b"],
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
- `providers.<name>.type` — internal adapter id (see AGENTS.md provider table). Required for custom aliases. Built-in providers can omit `type` when the key matches their adapter id.
- `providers.<name>.models` — list shown in UI; falls back to adapter `default_models` when omitted. Accepts a single string.
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
- Config-derived effort is applied only when `adapter.supports_reasoning_effort` AND `model_metadata.supports_reasoning` (from models.dev) are both true
- CLI `/effort` command and web sidebar allow per-request overrides without changing config
- See `docs/providers.md` for per-adapter mapping details

## Model Metadata

`mycode/core/models.py` fetches and caches `https://models.dev/api.json` to look up:

- `supports_reasoning` — whether the model supports extended thinking
- `context_window` — used for compact threshold calculation
- `max_output_tokens` — passed to the provider as the output limit; defaults to `8192` when not available

Cache: `~/.mycode/cache/models.dev-api.json`, TTL 24 hours. In-memory cache within the process.

Model lookup strategy (`lookup_model_metadata`):

1. Exact match on the given `provider_type` + raw model id
2. Fallback provider mapping (e.g., `claude-*` → `anthropic`, `deepseek-*` → `deepseek`)
3. Generic `aihubmix` catalog as last resort

## Skills Discovery

`mycode/core/system_prompt.py` scans for `SKILL.md` files and injects an `<available_skills>` block into the system prompt.

Scan roots (lowest to highest priority):

1. `~/.agents/skills/`
2. `~/.mycode/skills/`
3. `{cwd}/.agents/skills/`
4. `{cwd}/.mycode/skills/`

Each `SKILL.md` requires YAML frontmatter with `name` and `description`. Later roots override earlier ones by skill name. Max scan depth: 3 directory levels, max 200 directories per root.

The model uses the `read` tool to load full skill content on demand from the skill `path`.

## Instructions Discovery

`mycode/core/system_prompt.py` reads `AGENTS.md` files and injects them as `<workspace_instructions>` into the system prompt. Files checked:

1. `~/.mycode/AGENTS.md` (fallback: `~/.agents/AGENTS.md`)
2. `{cwd}/AGENTS.md`

Later files are more specific and take precedence.

## Workspace Root

`find_workspace_root(cwd)` walks up from cwd to find a `.git` directory. Falls back to cwd itself. Used to locate `<workspace>/.mycode/config.json`.

## Sessions Directory

`resolve_sessions_dir()` → `~/.mycode/sessions/` (or `$MYCODE_HOME/sessions/`). See `docs/sessions.md`.

## Port

Server port: `PORT` env var → `settings.port` (default `8000`). Overridden by `--port` CLI flag.
