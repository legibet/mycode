# mycode

>There are many coding agents, but this one is mine.

A minimal coding agent. Inspired by [pi](https://github.com/badlogic/pi-mono).

- Minimal agent core (< 5k lines) with multiple provider support and robust message replay.
- 4 built-in tools only (`read`, `write`, `edit`, `bash`), expanded via skills.
- Mobile-friendly web UI.
- Native image and pdf input support.
- Lightweight Python SDK for building custom agents.

## Quick Start

Requires Python 3.12+. Install via [uv](https://docs.astral.sh/uv/):

```bash
uv tool install mycode-cli
```

Interactive terminal UI:

```bash
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

API keys are discovered automatically from environment variables (see Providers).

## Providers

| Provider          | id            | Env var              |
| ----------------- | ------------- | -------------------- |
| Anthropic         | `anthropic`   | `ANTHROPIC_API_KEY`  |
| OpenAI            | `openai`      | `OPENAI_API_KEY`     |
| Google Gemini     | `google`      | `GEMINI_API_KEY`     |
| Moonshot          | `moonshotai`  | `MOONSHOT_API_KEY`   |
| MiniMax           | `minimax`     | `MINIMAX_API_KEY`    |
| DeepSeek          | `deepseek`    | `DEEPSEEK_API_KEY`   |
| Z.AI              | `zai`         | `ZAI_API_KEY`        |
| OpenRouter        | `openrouter`  | `OPENROUTER_API_KEY` |
| OpenAI-compatible | `openai_chat` | —                    |

Run `/model` in tui to see the available models.

## Configuration

A config file is optional — API keys from the environment are usually sufficient.

Create `~/.mycode/config.json` (global) or `<workspace>/.mycode/config.json` (project, takes precedence) to:

- set a default provider, model, and reasoning effort
- expose additional models on an existing provider (e.g. OpenRouter's catalog)
- register a custom endpoint, such as a private or regional deployment

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

- To override a built-in provider, reuse its id as the key — no `type` needed. Custom providers must declare a `type` — one of the built-in protocols.
- `reasoning_effort` controls extended thinking for supported models: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`.
- API keys in config accept `${ENV_VAR}` references.
- Model metadata is bundled from [models.dev](https://models.dev) — `{}` is enough for most models. Provide explicit fields only for models not listed there.

> Built-in Moonshot, MiniMax, and Z.AI providers default to international endpoints. Override `base_url` for China endpoints.

## CLI Reference

```bash
mycode                            start interactive session (new)
mycode --continue                 resume the most recent session
mycode --session <id>             resume a specific session
mycode run "..."                  send one message, non-interactive
mycode web                        start web server (default port 8000)
mycode web --dev                  API only, no static files
mycode session list               list saved sessions
```

Interactive slash commands: `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/clear` `/q`

## Development

```bash
git clone https://github.com/legibet/mycode.git && cd mycode
uv sync --dev
uv run mycode
```

Web development (backend + Vite dev server):

```bash
uv run mycode web --dev
pnpm --dir web install && pnpm --dir web dev
```

Or start both together:

```bash
just dev
```

Other useful shortcuts: `just check` · `just test` · `just fmt`

Build distributable artifacts:

```bash
uv build --package mycode-sdk
uv build --package mycode-cli
```

## mycode-sdk

Agent core as a lightweight Python SDK for building agents. Install via: `uv add mycode-sdk`

```python
import asyncio
from mycode import Agent, bash_tool, read_tool

async def main() -> None:
    agent = Agent(
        model="claude-sonnet-4-6",
        api_key="...",
        cwd=".",
        system="You are a concise coding assistant.",
        tools=[read_tool, bash_tool],
    )
    async for event in agent.achat("Read pyproject.toml and tell me the project name."):
        if event.type == "text":
            print(event.data["delta"], end="")

asyncio.run(main())
```

See [mycode/README.md](mycode/README.md) for details.

## License

MIT
