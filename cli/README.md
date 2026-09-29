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
| Alibaba Cloud     | `alibaba`     | `DASHSCOPE_API_KEY`  |
| xAI               | `xai`         | `XAI_API_KEY`        |
| OpenAI-compatible | `openai_chat` | -                    |

Run `/model` in the TUI to see available models.

## Configuration

A config file is optional — API keys from the environment are usually enough. Create `~/.mycode/config.json` (global) or `.mycode/config.json` in a project to customize further.

Expose additional models on an existing provider, or register a custom endpoint such as a private or regional deployment:

```json
{
  "providers": {
    "openrouter": {
      "models": {"deepseek/deepseek-v4-pro": {}}
    },
    "my-endpoint": {
      "type": "openai_chat",
      "base_url": "https://example.com/v1",
      "api_key": "${MY_API_KEY}",
      "models": {"my-model": {}}
    }
  }
}
```

A `providers` key matching a built-in provider id overrides it; other names are custom providers and must declare a `type` (one of the ids in the table above). `api_key` accepts a literal value or a `${ENV_VAR}` reference. `{}` is enough for models covered by the bundled [models.dev](https://models.dev) metadata; add fields only for models it doesn't list.

> Built-in Moonshot, MiniMax, and Z.AI providers default to international endpoints. Override `base_url` for China endpoints.

See [docs/config.md](https://github.com/legibet/mycode/blob/main/docs/config.md) for the full schema — permission levels, web providers, and resolution rules.

## Skills and instructions

The CLI assembles the system prompt from instructions files and discovered skills.

- `AGENTS.md` files are injected as project instructions: `~/.mycode/AGENTS.md` (fallback `~/.agents/AGENTS.md`), then every `AGENTS.md` from the project root down to the current directory. Later files take precedence.
- Skills are directories containing a `SKILL.md`. Scan roots, lowest to highest priority: `~/.agents/skills/`, `~/.mycode/skills/`, then `.agents/skills/` and `.mycode/skills/` from the project root down to the current directory. Later roots override earlier ones by skill name. `SKILL.md` needs YAML frontmatter with `name` and `description`.
- A standalone `/<skill-name>` token in a message loads the matching skill, e.g. `Use /fastapi to review this route`. Names matching built-in slash commands are reserved.

## Terminal UI

| Key                          | Action                                                                                 |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| Enter                        | Send the message (steer while a reply runs); with a completion menu open, accept it    |
| Ctrl+Q                       | Queue the message for after the reply; otherwise send it                               |
| Alt+Up                       | Take back pending steers and queued messages into the input                            |
| Esc then Enter, or Alt+Enter | Insert a newline                                                                       |
| Esc                          | Take back pending messages and interrupt the reply; close a selection list             |
| Ctrl+C                       | Take back pending messages and interrupt the reply; otherwise clear the input          |
| Ctrl+D                       | Exit (empty input only)                                                                |
| Ctrl+L                       | Clear the screen                                                                       |
| Up / Down                    | Move between lines; on the first or last line, browse input history                    |

While a reply runs, Enter steers it: the model sees the message at its next step, after the running tools finish. Ctrl+Q queues the message instead; queued messages are sent together as the next turn when the reply finishes. Pending messages show above the input. Commands are unavailable until the reply ends.

`@path` attaches a file to the message: text files as snapshots, images and PDFs as image or document input. Pasted file paths become `@path` references.

Slash commands: `/new` (alias `/clear`) `/resume` `/rewind` `/model` `/effort` `/compact` `/quit` (alias `/exit`). `exit` and `quit` also exit.

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

## License

MIT
