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
mycode web [--port <port>] [--host <host>]
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

Run `/model` in the TUI to see available models. `--model` takes a listed model id or `provider/model` (for example `--model deepseek/deepseek-v4-pro`); see [docs/cli.md](https://github.com/legibet/mycode/blob/main/docs/cli.md#model-selection).

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
- A skill is a directory containing a `SKILL.md`, up to three levels below a scan root, or a `.md` file directly in a scan root. Scan roots, lowest to highest priority: `~/.agents/skills/`, `~/.mycode/skills/`, then `.agents/skills/` and `.mycode/skills/` from the project root down to the current directory. Later roots override earlier ones by skill name. The file needs YAML frontmatter with a `description`; `name` defaults to the directory or file name and may contain only letters, digits, `-`, and `_` (at most 64 characters).
- A standalone `/<skill-name>` token in a message loads the matching skill, e.g. `Use /fastapi to review this route`. Names matching built-in slash commands are reserved.

## Subagents

The main agent can hand a self-contained task to a subagent. Subagents follow the session's permission settings, but one running in the background cannot ask for approval, so its calls that need approval are denied.

The built-in `general` subagent has the same tools as the main agent. Add your own as markdown files in `~/.mycode/agents/` or in `.mycode/agents/` of the project. A file named `general.md` replaces the built-in.

```markdown
---
description: Reviews a diff for correctness bugs
model: anthropic/claude-sonnet-5
permission: readonly
max_turns: 30
---
You review code changes. Report bugs with file and line.
```

`description` is required: it tells the model what the subagent is for. The body is added to the subagent's system prompt. `permission` takes the same values as the config `permission` and can only restrict the session's.

To run subagents on other models, list them in the config. The first is the default for subagents whose definition names no `model`, and the model can pick any of them for each subagent:

```json
{"subagent": {"models": ["deepseek/deepseek-v4-flash", "anthropic/claude-opus-5"]}}
```

Reference: [docs/subagents.md](https://github.com/legibet/mycode/blob/main/docs/subagents.md).

## Terminal UI

| Key                          | Action                                                                                 |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| Enter                        | Send the message (steer while a reply runs); with a completion menu open, accept it    |
| Ctrl+Q                       | Queue the message for after the reply; otherwise send it                               |
| Alt+Up                       | Take back pending steers and queued messages into the input                            |
| Shift+Enter or Alt+Enter     | Insert a newline (Esc then Enter also works)                                           |
| Esc                          | Take back pending messages and interrupt the reply; close a selection list             |
| Ctrl+C                       | Take back pending messages and interrupt the reply; otherwise clear the input          |
| Ctrl+D                       | Exit (empty input only)                                                                |
| Ctrl+L                       | Clear the screen                                                                       |
| Up / Down                    | Move between lines; on the first or last line, browse input history                    |

While a reply runs, Enter steers it: the model sees the message at its next step, after the running tools finish. Ctrl+Q queues the message instead; queued messages are sent together as the next turn when the reply finishes. Pending messages show above the input. Commands are unavailable until the reply ends.

A `bash` call with `background=true` keeps running after the reply ends (`docs/tools.md`). Its result is steered into the running reply, or wakes the idle session as a new turn, printed as a `Background finished` card with the command, its exit code, and the output tail. Esc, Ctrl+C, or denying a tool call holds wakes until the next message, which then carries the waiting results. `/new`, `/rewind`, a completed `/resume`, and exit stop the session's background commands and agents.

`/agents` lists the session's subagents. Choosing one prints its transcript; a running one can also be stopped.

`@path` attaches a file to the message: text files as snapshots, images and PDFs as image or document input. A large image is scaled down. A file the message cannot hold (an image or PDF over 20MB, a PDF over 20 pages, any other binary file) is passed as its path for the agent to inspect. Pasted file paths become `@path` references.

Slash commands: `/new` (alias `/clear`) `/resume` `/rewind` `/model` `/effort` `/compact` `/agents` `/quit` (alias `/exit`). `exit` and `quit` also exit.

## CLI Reference

```bash
mycode                            start interactive session (new)
mycode --continue                 resume the most recent session
mycode --session <id>             resume a specific session
mycode --effort high              set effort for this session
mycode run "..."                  send one prompt, print the reply, exit
git diff | mycode run "review"    piped stdin is appended to the prompt
mycode run --format json "..."    machine-readable result (also: jsonl)
mycode web                        start web server (default port 8000, or $PORT)
mycode web --dev                  API only, with hot reload
mycode session list [--all]       list saved sessions
mycode -h                         help; also works on every subcommand
```

Options before a subcommand configure the interactive session only; pass `run` options after `run`. Full reference, output formats, and exit codes: [docs/cli.md](https://github.com/legibet/mycode/blob/main/docs/cli.md).

## License

MIT
