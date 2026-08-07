# mycode-cli

Interactive coding agent CLI and web server, built on [mycode-sdk](https://github.com/legibet/mycode/blob/main/mycode/README.md).

## Quick Start

Requires Python 3.12+. Install via [uv](https://docs.astral.sh/uv/):

```bash
uv tool install mycode-cli
```

Set a provider API key, then start:

```bash
export ANTHROPIC_API_KEY=...
mycode
```

Web UI (default at `http://localhost:8000`):

```bash
mycode web [--port <port>] [--hostname <hostname>]
```

Single message, non-interactive:

```bash
mycode run "explain how the session store works"
```

## Providers

| Provider | id | Env var |
| --- | --- | --- |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Google Gemini | `google` | `GEMINI_API_KEY` |
| Moonshot | `moonshotai` | `MOONSHOT_API_KEY` |
| MiniMax | `minimax` | `MINIMAX_API_KEY` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| Z.AI | `zai` | `ZAI_API_KEY` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` |
| Alibaba Cloud | `alibaba` | `DASHSCOPE_API_KEY` |
| xAI | `xai` | `XAI_API_KEY` |
| OpenAI-compatible | `openai_chat` | - |

Run `/model` in the TUI to see available models.

## Configuration

A config file is optional. API keys from the environment are usually sufficient.

Create `~/.mycode/config.json` (global) or `.mycode/config.json` under the current project to:

- set a default provider and model
- expose additional models on an existing provider (e.g. OpenRouter's catalog)
- register a custom endpoint, such as a private or regional deployment

```json
{
  "default": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6"
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
          "supports_image_input": false
        }
      }
    }
  }
}
```

- To override a built-in provider, reuse its id as the key. No `type` needed. Custom providers must declare a `type`, one of the built-in protocols.
- API keys in config accept `${ENV_VAR}` references.
- Model metadata is bundled from [models.dev](https://models.dev). `{}` is enough for most models. Provide explicit fields only for models not listed there.

> Built-in Moonshot, MiniMax, and Z.AI providers default to international endpoints. Override `base_url` for China endpoints.

See [docs/config.md](https://github.com/legibet/mycode/blob/main/docs/config.md) for the full schema and resolution rules.

## CLI Reference

```bash
mycode                            start interactive session (new)
mycode --continue                 resume the most recent session
mycode --session <id>             resume a specific session
mycode run "..."                  send one message, non-interactive
mycode run --effort high "..."     set effort for one run
mycode web                        start web server (default port 8000)
mycode web --dev                  API only, no static files
mycode session list               list saved sessions
```

Interactive slash commands: `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/clear` `/compact` `/q`

## License

MIT
