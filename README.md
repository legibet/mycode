# mycode

> There are many coding agents, but this one is mine.

A minimal coding agent.

- Minimal (~10k lines) but complete.
- Multiple provider support and robust message replay.
- 4 built-in tools (`read`, `write`, `edit`, `bash`), expanded via skills.
- Mobile-friendly web UI.
- Native image and pdf input support.

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

API keys are discovered automatically from environment variables. Run `/model` in the TUI to see available models.

## Providers

Supports Alibaba Cloud, Anthropic, OpenAI, Google Gemini, DeepSeek, Moonshot, MiniMax, Z.AI, OpenRouter, xAI, and any OpenAI-compatible endpoint. Set the provider's env var and you're ready.

See [cli/README.md](cli/README.md) for the full provider table and configuration.

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

Interactive slash commands: `/new` `/resume` `/rewind` `/provider` `/model` `/effort` `/clear` `/compact` `/q`

## Development

The `web/` UI is a git submodule from [`legibet/mycode-web`](https://github.com/legibet/mycode-web); `--recurse-submodules` fetches it.

```bash
git clone --recurse-submodules https://github.com/legibet/mycode.git && cd mycode
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

Agent core of mycode as a lightweight Python SDK for building custom agents. Install via `uv add mycode-sdk`.

```python
from mycode import Agent, read_tool

agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    tools=[read_tool],
)

result = agent.run("Read pyproject.toml and tell me the project name.")
print(result.text)
```

See [mycode/README.md](mycode/README.md) for details.

## License

MIT
