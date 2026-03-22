# mycode

A personal minimal coding agent (Python) with a web UI.

Design principles (inspired by pi):

- **Minimal core**: only 4 tools: `read`, `write`, `edit`, `bash`
- **Clean agent loop**: streaming + tool loop, no extra framework features
- **Low token overhead**: truncation everywhere, explicit "use rg" guidance
- **Sessions are append-only**: JSONL message log per session

## CLI

```bash
mycode
```

Send one message and exit:

```bash
mycode run "Explain how the session store works"
```

## Web

Installed packages include the bundled web UI. Start the server with:

```bash
mycode web
```

## Development

Install the project in editable mode with uv:

```bash
uv sync --dev
```

For TUI and CLI development, the editable install is enough:

```bash
uv run mycode
```

For web development, run the backend in API-only mode and start the Vite dev server separately:

```bash
uv run mycode web --dev
pnpm --dir frontend install
pnpm --dir frontend dev
```

In this mode, the backend does not depend on `mycode/server/static`, and the Vite dev server proxies API requests to `http://127.0.0.1:8000`.

When you need to refresh the packaged frontend assets from the repository, build and sync them with:

```bash
uv run --no-project python scripts/build_frontend.py
```

`uv build` also runs the frontend build automatically and packages the static assets into the wheel/sdist.

## Release

Build distributable artifacts with:

```bash
uv build
```

Users can install a published release with:

```bash
uv tool install mycode
```

For a locally built artifact, install the wheel with:

```bash
uv tool install dist/mycode-0.1.0-py3-none-any.whl
```

Installed users do not need Node.js or pnpm. `mycode web` serves the bundled frontend directly from the Python package.

## Common Workflows

- Installed user: `uv tool install mycode`, then run `mycode` or `mycode web`
- Python/CLI development: `uv sync --dev`, then run `uv run mycode`
- Web development: run `uv run mycode web --dev` and `pnpm --dir frontend dev` in separate terminals
- Refresh packaged frontend assets in the repository: `uv run --no-project python scripts/build_frontend.py`
- Release a distributable build: `uv build`

## Runtime Data

Persistent runtime data lives under `~/.mycode/`:

- `config.json`
- `cli_history`
- `sessions/`

## Config

Config files are loaded from:

- `~/.mycode/config.json`
- `<workspace>/.mycode/config.json`

`provider` can be either:

- a configured alias from `providers`
- a raw built-in provider id such as `anthropic`, `moonshotai`, `minimax`, `openai`, or `openai_chat`

Example:

```json
{
  "default": {
    "provider": "moonshotai",
    "model": "kimi-k2.5"
  },
  "providers": {
    "moonshotai": {
      "type": "moonshotai",
      "base_url": "https://api.moonshot.ai/anthropic",
      "models": ["kimi-k2.5"]
    },
    "minimax": {
      "type": "minimax",
      "base_url": "https://api.minimax.io/anthropic",
      "models": ["MiniMax-M2.5", "MiniMax-M2.5-highspeed"]
    },
    "claude": {
      "type": "anthropic",
      "base_url": "https://api.anthropic.com",
      "models": ["claude-sonnet-4-6"]
    },
    "openrouter": {
      "type": "openai_chat",
      "base_url": "https://openrouter.ai/api/v1",
      "models": ["openai/gpt-4.1-mini"]
    }
  }
}
```

`type` is the internal adapter id used at runtime:

- `anthropic` / `moonshotai` / `minimax` use the official Anthropic SDK and the Messages API
- `openai` uses the official OpenAI SDK and the Responses API
- `openai_chat` uses the official OpenAI SDK and the Chat Completions API for third-party OpenAI-compatible providers

If you do not need aliases, you can skip config and pass the raw provider directly:

```bash
mycode --provider moonshotai --model kimi-k2.5
```

Region-specific note:

- built-in Moonshot defaults use the international endpoint `https://api.moonshot.ai/anthropic`
- built-in MiniMax defaults use the international endpoint `https://api.minimax.io/anthropic`
- if you use the China endpoints instead, override `base_url` in config or pass `api_base`

## Environment Variables

Provider/model/base URL are not loaded from environment variables.

API keys:

- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `MOONSHOT_API_KEY` / `MINIMAX_API_KEY`
- `providers.<name>.api_key` also supports exact env references like `${OPENROUTER_API_KEY}`
- when config uses `${ENV_NAME}`, that referenced env var is used before the provider's built-in default API key env var
- if `${ENV_NAME}` is configured but missing at runtime, startup fails with a clear error
- Or pass `api_key` from the web UI (per request)
