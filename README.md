# mycode-go

>There are many coding agents, but this one is mine.

A minimal coding agent. Inspired by [pi](https://github.com/badlogic/pi-mono).

- Minimal core.
- Unified message format and robust cross-provider replay.
- 4 built-in tools: `read`, `write`, `edit`, `bash`, expanded via skills.
- Inspectable runtime, append-only JSONL sessions.
- Native image and pdf input support.
- Mobile-friendly web UI.

This repository is the Go rewrite of the original backend. The web API, SSE contract, session format, provider ids, and core runtime behavior stay aligned with the Python version. The old terminal TUI is not included in this rewrite. The CLI and binary name are `mycode-go`. Config and session directories stay compatible with the original `.mycode` layout.

Repository layout:

- `mycode-go/` — Go module, CLI, server, runtime
- `web/` — React + Vite frontend source
- `dist/` — built binaries

## Quick Start

Build a user-ready binary with the web UI embedded:

```bash
make build
```

Single message, non-interactive:

```bash
./dist/mycode-go "explain how the session store works"
./dist/mycode-go run "explain how the session store works"
```

Resume the latest session in the current workspace:

```bash
./dist/mycode-go run --continue "continue the last task"
```

Web UI (default at `http://127.0.0.1:8000`):

```bash
./dist/mycode-go web (--port <port> --hostname <hostname>)
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

All four interface families use official Go SDKs:

- Anthropic Messages API and Anthropic-compatible endpoints: `github.com/anthropics/anthropic-sdk-go`
- OpenAI Responses API: `github.com/openai/openai-go/v3`
- OpenAI Chat Completions and compatible chat endpoints: `github.com/openai/openai-go/v3`
- Google Gemini: `google.golang.org/genai`

## Configuration

No config file is required. It is only used for:

1. Setting default provider, model, and other options
2. Overriding built-in provider settings
3. Adding custom providers with any built-in provider type
4. Customizing model metadata for built-in and custom models

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
          "supports_image_input": false,
          "supports_pdf_input": false
        }
      }
    }
  }
}
```

- Built-in provider ids can be overridden by key without specifying `type`. Custom providers must set `type`.
- `reasoning_effort` controls extended thinking for supported models: `auto` (default) · `none` · `low` · `medium` · `high` · `xhigh`.
- API keys in config accept `${ENV_VAR}` references.
- Model metadata is bundled locally and can be overridden per model in config.

> Built-in Moonshot, MiniMax, and Z.AI defaults use international endpoints. Override `base_url` in config for China endpoints.

## CLI Reference

```bash
mycode-go "..."                      send one message, non-interactive
mycode-go run "..."                  send one message, non-interactive
mycode-go run --continue "..."       resume the most recent session in the current workspace
mycode-go run --session <id> "..."   resume a specific session
mycode-go web                        start web server (default port 8000)
mycode-go web --dev                  API only, no static files
mycode-go session list               list saved sessions in the current workspace
mycode-go session list --all         list saved sessions from all workspaces
```

## Development

```bash
git clone <repo> && cd mycode-go
make test-go
go -C mycode-go run ./cmd/mycode-go "hello"
```

Web development (backend + Vite dev server):

```bash
make web-install
make web-dev
pnpm --dir web dev
```

Sync web assets into the embedded static directory:

```bash
make web-build
```

Refresh the bundled model catalog from models.dev:

```bash
make update-models-catalog
```

Build the binary:

```bash
make build
```

## License

MIT
