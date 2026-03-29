# Configuration

## Config Files

Loaded in order (later values override earlier):

1. `~/.mycode/config.json` — global
2. `<workspace>/.mycode/config.json` — project-specific (found by walking up from cwd to `.git` root)

Explicit request args (CLI flags, API params) override both.

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

- `default.provider` references a key in `providers`, or a raw adapter id
- `providers.<name>.type` is the internal adapter id (see AGENTS.md provider table)
- `providers.<name>.models` — list shown in UI; falls back to adapter's built-in defaults when omitted
- `api_key` — literal value or `${ENV_NAME}` reference; when `${ENV_NAME}` is used, that env var takes priority over the provider's built-in default env var
- Provider aliases and custom `base_url` values are **not** auto-discovered from env; built-in providers themselves can still be selected from available API key env vars
- If no provider is configured and no API key env var is found, startup raises an error listing which env vars to set

## API Key Resolution Order

1. Explicit `api_key` value in config (or dereferenced `${ENV_NAME}`)
2. Provider's built-in default env vars (e.g., `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)

## Reasoning Effort

`reasoning_effort` controls how much thinking a model does.

Options: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`

- `auto` — do not send any effort parameter; let the provider decide
- `none` — explicitly disable thinking
- Resolution order: request override → `providers.<name>.reasoning_effort` → `default.reasoning_effort`
- Applied only when `adapter.supports_reasoning_effort` AND `model_metadata.supports_reasoning` are both true
- CLI `/effort` command and web sidebar allow per-request overrides without changing config

## Model Metadata

`mycode/core/models.py` fetches and caches `https://models.dev/api.json` to look up:

- `supports_reasoning` — whether the model supports extended thinking
- `context_window` — used for compact threshold calculation
- `max_output_tokens` — passed to the provider as the output limit

Cache location: `~/.mycode/cache/models.dev-api.json`, TTL 24 hours.

## Skills Discovery

`mycode/core/skills.py` scans for `SKILL.md` files and injects an `<available_skills>` block into the system prompt. Scan roots (lowest to highest priority):

1. `~/.agents/skills/`
2. `~/.mycode/skills/`
3. `{cwd}/.agents/skills/`
4. `{cwd}/.mycode/skills/`

Each `SKILL.md` requires YAML frontmatter with `name` and `description`. The model uses the `read` tool to load full skill content on demand.

## Instructions Discovery

`mycode/core/instructions.py` reads `AGENTS.md` files and injects them as `<workspace_instructions>` into the system prompt. Files checked (in order):

1. `~/.mycode/AGENTS.md` (or `~/.agents/AGENTS.md` as fallback)
2. `{cwd}/AGENTS.md`

Later files are more specific and take precedence in the model's interpretation.
