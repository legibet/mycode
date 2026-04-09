# Configuration

Source: `mycode-go/internal/config/`

## Config Files

Loaded in order (later values override earlier):

1. `~/.mycode/config.json` — global
2. `<workspace>/.mycode/config.json` — project-specific (found by walking up from cwd to the nearest `.git` root)

Explicit request args (CLI flags, API params) override both.

Config resolution: `config.Load(cwd)` returns `Settings`.

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
- `default.reasoning_effort` — global default; `null`, `"auto"`, and `"default"` all resolve to no override
- `default.compact_threshold` — fraction of context window that triggers compaction; `false` or `0` disables; range `[0, 1]`; default `0.8`
- `providers.<name>.type` — internal adapter id. Required for custom aliases. Built-in providers can omit `type` when the key matches their adapter id.
- `providers.<name>.models` — model map. Keys are model ids shown in the UI. Values can override bundled metadata for that exact model.
- `providers.<name>.models.<model>.context_window` — override the model context window
- `providers.<name>.models.<model>.max_output_tokens` — override the provider output limit
- `providers.<name>.models.<model>.supports_reasoning` — override whether reasoning effort is available
- `providers.<name>.models.<model>.supports_image_input` — override image input support
- `providers.<name>.models.<model>.supports_pdf_input` — override PDF input support
- `providers.<name>.api_key` — literal value or `${ENV_NAME}` reference
- `providers.<name>.base_url` — override the adapter's default base URL
- `providers.<name>.reasoning_effort` — per-provider override of the global default

## API Key Resolution Order

For a resolved provider (`ResolveProvider` in `mycode-go/internal/config/resolve.go`):

1. Explicit `api_key` param (CLI flag or API request)
2. Config `api_key`
   - `${ENV_NAME}` — dereferenced from env at resolution time
   - plain string — used as-is
3. Provider adapter's built-in default env vars (for example `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)

If no API key is found at any step, provider resolution returns an error listing which env vars were checked.

## Provider Resolution

`ResolveProvider(settings, providerName, model, apiKey, apiBase, reasoningEffort)` returns a `ResolvedProvider`:

1. If `providerName` is given: resolve it as a configured alias or raw provider id
2. If no provider is given: try the configured default
3. Fallback: iterate configured providers with valid credentials, then env-discoverable built-in providers
4. If nothing is found: return an error listing checked env vars

Auto-discovery is limited to providers where `AutoDiscoverable=true` and the corresponding env var is set.

Configured provider order and configured model order follow the order in the JSON file, matching the Python version.

## Reasoning Effort

Controls how much thinking a model does.

Config resolution: `providers.<name>.reasoning_effort` → `default.reasoning_effort`

Request override: `POST /api/chat` normalizes `reasoning_effort` and passes it through directly when set.

Options: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`

- `auto` — do not send any effort parameter; let the provider decide
- `none` — explicitly disable thinking
- Config-derived effort is applied only when `adapter.SupportsReasoningEffort` and `model_metadata.supports_reasoning` are both true
- See `docs/providers.md` for per-adapter mapping details

## Model Metadata

`mycode-go/internal/models/catalog.go` reads the bundled `mycode-go/internal/models/models_catalog.json` catalog to look up:

- `supports_reasoning` — whether the model supports extended thinking
- `supports_image_input` — whether the model accepts image input
- `supports_pdf_input` — whether the model accepts PDF input
- `context_window` — used for compact threshold calculation
- `max_output_tokens` — passed to the provider as the output limit; defaults to `16384` when not available
- `context_window` — defaults to `128000` when the bundled catalog does not provide one

Per-model config overrides are applied field by field on top of the bundled catalog.

Refresh the bundled catalog from `models.dev` with:

```bash
make update-models-catalog
```

## Skills Discovery

`mycode-go/internal/prompt/prompt.go` scans for `SKILL.md` files and injects an `<available_skills>` block into the system prompt.

Scan roots (lowest to highest priority):

1. `~/.agents/skills/`
2. `~/.mycode/skills/`
3. `{cwd}/.agents/skills/`
4. `{cwd}/.mycode/skills/`

Each `SKILL.md` requires YAML frontmatter with `name` and `description`. Later roots override earlier ones by skill name. Max scan depth: 3 directory levels, max 200 directories per root.

The model uses the `read` tool to load full skill content on demand from the skill `path`.

## Instructions Discovery

`mycode-go/internal/prompt/prompt.go` reads `AGENTS.md` files and injects them as `<workspace_instructions>` into the system prompt. Files checked:

1. `~/.mycode/AGENTS.md` (fallback: `~/.agents/AGENTS.md`)
2. `{cwd}/AGENTS.md`

Later files are more specific and take precedence.

## Workspace Root

`FindWorkspaceRoot(cwd)` walks up from cwd to find a `.git` directory. Falls back to cwd itself. Used to locate `<workspace>/.mycode/config.json`.

## Sessions Directory

`ResolveSessionsDir()` → `~/.mycode/sessions/` (or `$MYCODE_HOME/sessions/`). See `docs/sessions.md`.

## Port

Server port: `PORT` env var → `settings.Port` (default `8000`). Overridden by `--port` CLI flag.
