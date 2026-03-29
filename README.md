# mycode

*There are many coding agents, but this one is mine.*

A minimal coding agent with a web UI and TUI. Inspired by [pi](https://github.com/nicholasgasior/pi).

- 4 tools only: `read`, `write`, `edit`, `bash`.
- Expand capabilities via skills.
- One message format, one agent loop — across all providers.
- Inspectable runtime, append-only sessions.

## Quick Start

Requires Python 3.13+. Install:

```bash
uv tool install mycode
```

Interactive session:

```bash
export ANTHROPIC_API_KEY=sk-...
mycode
```

Web UI at `http://localhost:8000`:

```bash
mycode web
```

Single message, non-interactive:

```bash
mycode run "explain how the session store works"
```

API keys are discovered automatically from environment variables. No config file needed.

## Providers & Models

| Provider          | id            | Env var              | Default models                                     |
| ----------------- | ------------- | -------------------- | -------------------------------------------------- |
| Anthropic         | `anthropic`   | `ANTHROPIC_API_KEY`  | `claude-sonnet-4-6`, `claude-opus-4-6`             |
| OpenAI            | `openai`      | `OPENAI_API_KEY`     | `gpt-5.4`, `gpt-5.4-mini`                          |
| Google Gemini     | `google`      | `GEMINI_API_KEY`     | `gemini-3.1-pro-preview`, `gemini-3-flash-preview` |
| Moonshot          | `moonshotai`  | `MOONSHOT_API_KEY`   | `kimi-k2.5`                                        |
| MiniMax           | `minimax`     | `MINIMAX_API_KEY`    | `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`           |
| DeepSeek          | `deepseek`    | `DEEPSEEK_API_KEY`   | `deepseek-chat`, `deepseek-reasoner`               |
| Z.AI              | `zai`         | `ZAI_API_KEY`        | `glm-5.1`, `glm-5-turbo`                           |
| OpenRouter        | `openrouter`  | `OPENROUTER_API_KEY` | `openai/gpt-5.2`, `anthropic/claude-sonnet-4.6`    |
| OpenAI-compatible | `openai_chat` | —                    | (configured per provider)                          |

Providers with an env var are auto-discoverable — set the key and pass `--provider <id>` to use them without any config file.

`openai_chat` is a generic adapter for any OpenAI-compatible Chat Completions endpoint. Configure it with a custom `base_url` (see Configuration).

## Configuration

No config file is required. It is only used for three things:

1. Setting default provider, model, and other options
2. Overriding built-in provider settings (e.g. changing the default model list)
3. Adding custom providers backed by `openai_chat`

Config is loaded from `~/.mycode/config.json` (global) and `<workspace>/.mycode/config.json` (project-specific, takes precedence).

```json
{
  "default": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "reasoning_effort": "medium"
  },
  "providers": {
    "anthropic": {
      "type": "anthropic",
      "models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"]
    },
    "my-openrouter": {
      "type": "openai_chat",
      "base_url": "https://openrouter.ai/api/v1",
      "api_key": "${OPENROUTER_API_KEY}",
      "models": ["openai/gpt-4.1", "google/gemini-2.5-pro"]
    }
  }
}
```

`reasoning_effort` controls extended thinking for supported models: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`.

API keys in config accept `${ENV_VAR}` references. Provider, model, and base URL are not loaded from environment variables automatically — pass them as flags or set them in config:

```bash
mycode --provider anthropic --model claude-opus-4-6
```

> Built-in Moonshot and MiniMax defaults use international endpoints. Override `base_url` in config for China endpoints.

## CLI Reference

```
mycode                            start interactive session (new)
mycode --continue                 resume the most recent session
mycode --session <id>             resume a specific session
mycode run "..."                  send one message, non-interactive
mycode web                        start web server (default port 8000)
mycode web --dev                  API only, no static files
mycode session list               list saved sessions
```

Interactive slash commands: `/new` `/resume` `/provider` `/model` `/effort` `/clear` `/q`

## Development

```bash
git clone <repo> && cd mycode
uv sync --dev
uv run mycode
```

Web development (backend + Vite dev server):

```bash
uv run mycode web --dev
pnpm --dir frontend install && pnpm --dir frontend dev
```

Rebuild packaged frontend assets:

```bash
uv run --no-project python scripts/build_frontend.py
```

Build distributable artifacts:

```bash
uv build
```

## License

MIT
