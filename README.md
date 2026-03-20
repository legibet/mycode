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

Run one prompt and exit:

```bash
mycode run "Explain how the session store works"
```

## Web

Build the frontend first if you want the bundled web UI:

```bash
pnpm --dir mycode/frontend install
pnpm --dir mycode/frontend build
```

Then start the server:

```bash
mycode web
```

If the frontend is not built yet, `mycode web` still starts the API server and prints a warning.

## Run (frontend)

```bash
pnpm --dir mycode/frontend install
pnpm --dir mycode/frontend dev
```

Configure the backend URL via Vite proxy if needed.

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
