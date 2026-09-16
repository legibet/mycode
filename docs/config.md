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
    "model": "claude-sonnet-5",
    "compact_threshold": 0.8
  },
  "permission": {
    "level": "safe",
    "mode": "ask"
  },
  "web": {
    "fetch": "local",
    "search": "off",
    "tavily": {"api_key": "${TAVILY_API_KEY}"},
    "exa": {"api_key": "..."}
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
- `web.fetch` — `local`, `tavily`, or `exa`; default `local`. `webfetch` is always registered.
- `web.search` — `off`, `tavily`, or `exa`; default `off`. `websearch` is registered only when a provider is selected. Use explicit `off` in a project config to override a provider inherited from the global config.
- `web.tavily.api_key` / `web.exa.api_key` — literal value or `${ENV_NAME}` reference, shared by fetch and search for that provider
- `providers.<name>.type` — internal adapter id (see `docs/providers.md`). Required for custom aliases. Built-in providers can omit `type` when the key matches their adapter id.
- `providers.<name>.models` — model map. Keys are model ids shown in UI. Values can override the bundled model metadata for that exact model.
- `providers.<name>.models.<model>.context_window` — override the model context window
- `providers.<name>.models.<model>.max_output_tokens` — override the provider output limit
- `providers.<name>.models.<model>.reasoning_efforts` — override the model's available effort values; an empty list disables effort selection for that model
- `providers.<name>.models.<model>.supports_image_input` — override image input support
- `providers.<name>.models.<model>.supports_pdf_input` — override PDF input support
- `providers.<name>.api_key` — literal value or `${ENV_NAME}` reference
- `providers.<name>.base_url` — override the adapter's default base URL
- `providers.<name>.supports_reasoning_effort` — opt-in (default `false`) for a generic `openai_chat` endpoint that accepts the standard top-level `reasoning_effort`. Ignored for other provider types, which declare effort support in their adapter
- `providers.<name>.legacy_max_tokens` — opt-in (default `false`) for a generic `openai_chat` endpoint that only implements the legacy `max_tokens` field and would silently drop the default `max_completion_tokens`. Ignored for other provider types, which send the field their endpoint defines

## Provider Authentication Resolution

For a resolved provider (`_resolve_provider_runtime` in `config.py`):

1. Explicit `api_key` param (CLI flag or API request)
2. Config `api_key`
   - `${ENV_NAME}` — dereferenced from env at resolution time
   - plain string — used as-is
3. Provider adapter's built-in default env vars (e.g., `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`)

If no API key is found at any step and the adapter cannot authenticate from ambient environment state (`can_authenticate_from_env()`), resolution raises an error listing which env vars were checked. Otherwise the provider resolves with `api_key` unset — e.g. a configured `google_vertex` entry using ADC via `GOOGLE_CLOUD_PROJECT`.

Web provider keys follow the same explicit-reference behavior:

1. Literal `web.<provider>.api_key`
2. Explicit `${ENV_NAME}`; an unset reference is a call-time configuration error
3. When `api_key` is omitted, `TAVILY_API_KEY` or `EXA_API_KEY`
4. Tavily uses its keyless mode when still unset; Exa returns a call-time configuration error

Loading config and building an agent do not require web keys. Selecting a web provider is the opt-in; provider errors never switch to local or another provider.

## Provider Resolution

`resolve_provider(settings, provider_name=..., model=...)` returns a `ResolvedProvider`:

1. If `provider_name` given: resolve it as a configured alias or raw provider id; failures raise.
2. If no `provider_name`: try the configured default; failures fall through to step 3.
3. Iterate configured providers with available authentication, then env-discoverable built-in providers.
4. If nothing found: raise error listing checked env vars.

For configured entries, availability is resolved in this order:

1. If `api_key` is an explicit `${ENV_NAME}` reference, that environment variable must be set.
2. A literal `api_key` makes the entry available.
3. Otherwise, use the adapter's `can_authenticate_from_env()` result.

Auto-discovery is narrower: only providers with `auto_discoverable=True` and a built-in API key env var set; `can_authenticate_from_env()` is not consulted. So `GOOGLE_CLOUD_API_KEY` auto-discovers `google_vertex` while `GOOGLE_CLOUD_PROJECT` alone does not — ADC users opt in with a configured entry such as `{"type": "google_vertex"}`, which `GOOGLE_CLOUD_PROJECT` then makes available.

`ResolvedProvider.model_config` is the selected model's config override, or `None`.

## Reasoning Effort

Controls how much thinking a model does.

Available values come from the selected model's metadata or its `reasoning_efforts` override. TUI and Web prepend `auto`; models without values show no effort control. Without an explicit request or a frontend model-specific preference, effort is `auto`.

- `mycode run --effort <level>` sets effort for one non-interactive run; omitted means `auto`
- TUI `/effort` and the Web input control remember effort per provider/model without changing config; TUI preferences live in `~/.mycode/tui.json`
- See `docs/providers.md` for per-adapter mapping details

## Tool Permissions

`permission.level` controls which tool calls run without asking:

- `readonly` — clear read-only actions under `project`, discovered skill reads, and simple read-only shell commands
- `safe` — `readonly` plus `project`-local `write`/`edit`
- `standard` — `safe` plus ordinary single shell commands
- `yolo` — allow all tool calls

With `mode: "ask"` the TUI and web prompt for approval outside the level; `"deny"` rejects without prompting. The classification rules — compound and destructive shell commands, the webfetch trust boundary — are specified in `docs/tools.md`.

## Project Boundary

Config, instructions, and skill discovery walk from `project` to `cwd`, so nearer files have higher priority. Tool permissions treat paths inside `project` as project-local and require approval for paths outside `project`.

## Sessions Directory

`resolve_sessions_dir()` → `~/.mycode/sessions/` (or `$MYCODE_HOME/sessions/`). See `docs/sessions.md`.

## Port

Server port: `PORT` env var → `settings.port` (default `8000`). Overridden by `--port` CLI flag.
