# mycode

>There are many coding agents, but this one is mine.

A minimal coding agent. Inspired by [pi](https://github.com/badlogic/pi-mono).

- Minimal core (under 5k lines of code).
- Unified message format and robust cross-provider replay.
- 4 built-in tools: `read`, `write`, `edit`, `bash`, expanded via skills.
- Inspectable runtime, append-only JSONL sessions.
- Native image input.
- Mobile-friendly web UI.

## Quick Start

Requires Python 3.12+. Install via [uv](https://docs.astral.sh/uv/):

```bash
uv tool install mycode-cli
```

Interactive terminal session:

```bash
mycode
```

Web UI (default at `http://localhost:8000`):

```bash
mycode web (--port <port> --hostname <hostname>)
```

Single message, non-interactive:

```bash
mycode run "explain how the session store works"
```

API keys are discovered automatically from environment variables (see Providers & Models).

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
| OpenRouter        | `openrouter`  | `OPENROUTER_API_KEY` | `openrouter/auto`                                  |
| OpenAI-compatible | `openai_chat` | —                    | (configured per provider)                          |

## Configuration

No config file is required. It is only used for:

1. Setting default provider, model, and other options
2. Overriding built-in provider settings (e.g. changing the available model list)
3. Adding custom providers with any built-in provider type.
4. Customize model metadata for built-in and custom models.

Config is loaded from `~/.mycode/config.json` (global) and `<workspace>/.mycode/config.json` (project-specific, takes precedence).

```json
{
  "default": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "reasoning_effort": "medium"
  },
  "providers": {
    "openrouter": {
      "models": {
        "deepseek/deepseek-v3.2": {},
        "xiaomi/mimo-v2-pro": {}
      }
    },
    "zhipu-coding-plan": {
      "type": "zai",
      "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
      "api_key": "${ZHIPU_API_KEY}"
    },
    "custom-provider": {
      "type": "openai_chat",
      "base_url": "https://custom-endpoint.com/v1",
      "api_key": "${CUSTOM_API_KEY}",
      "models": {
        "custom-model": {
          "context_window": 128000,
          "max_output_tokens": 16384,
          "supports_reasoning": true,
          "supports_image_input": false
        }
      }
    }
  }
}
```

- Built-in provider ids can be overridden by key without specifying `type`. Custom providers must set `type`.
- `reasoning_effort` controls extended thinking for supported models: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`.
- API keys in config accept `${ENV_VAR}` references.
- Model metadata is sourced from models.dev and bundled — no manual config needed for built-in models.

> Built-in Moonshot, MiniMax, and Z.AI defaults use international endpoints. Override `base_url` in config for China endpoints.

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
