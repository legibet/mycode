# mycode-sdk

Lightweight Python SDK for building AI agents. Multi-turn conversations, tool calling, session persistence, and streaming events. Provider adapters for Anthropic, OpenAI, Google, and more.

## Install

```bash
uv add mycode-sdk
# or
pip install mycode-sdk
```

## Quick start

```python
import asyncio

from mycode import Agent, bash_tool, read_tool


async def main() -> None:
    agent = Agent(
        model="claude-sonnet-4-6",
        api_key="YOUR_API_KEY",
        tools=[read_tool, bash_tool],
    )

    async for event in agent.achat("Read pyproject.toml and tell me the project name."):
        if event.type == "text":
            print(event.data["delta"], end="", flush=True)


asyncio.run(main())
```

`Agent(...)` infers the provider from the model id. No tools are registered unless you pass `tools=[...]`.

For a synchronous call, use `run()`:

```python
result = agent.run("Read pyproject.toml and tell me the project name.")
print(result.text)
```

## Providers

The SDK infers the provider from the model string (`claude-*` to Anthropic, `gpt-*` to OpenAI, etc.). API keys are auto-discovered from environment variables:

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

Pass `api_key=` to override the env var, `api_base=` for a custom endpoint. Model metadata is bundled from [models.dev](https://models.dev); pass `context_window`, `supports_reasoning`, `supports_image_input`, or `supports_pdf_input` to override.

## Multi-turn conversations

Call `achat()` or `run()` again on the same `Agent` to continue:

```python
agent = Agent(model="claude-sonnet-4-6", api_key="...")

agent.run("What is 2 + 2?")
agent.run("Now multiply that by 10.")    # remembers the earlier answer
```

`agent.clear()` drops in-memory history. `agent.messages` accumulates across calls.

## Attachments

Pass `attachments` to `achat()` or `run()` to add files alongside the prompt:

```python
from mycode import Attachment

agent.run("Describe these.", attachments=["diagram.png", "report.pdf", "notes.txt"])

# Or build them explicitly:
agent.run(
    "Review.",
    attachments=[
        Attachment.path("diagram.png"),
        Attachment.bytes(png_data, media_type="image/png"),
        Attachment.text("TODO: ship it", name="note.md"),
    ],
)
```

Images support `image/png`, `image/jpeg`, `image/gif`, `image/webp`; documents support `application/pdf`. Sending an image or PDF to a model without that capability yields an `error` event. A bad path or unsupported type raises `ValueError` before the provider is called.

## Saving sessions

Pass `session_dir` to persist the conversation to disk. Each session lives in a subdirectory named by `session_id`:

```python
from pathlib import Path

agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    session_dir=Path("./chats"),
    session_id="my-chat",
)
```

Construct another `Agent` with the same `(session_dir, session_id)` to load the conversation history.

## Built-in tools

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

Four tools for reading, writing, editing files, and running shell commands. Opt in by passing them to `tools=[...]`.

## Custom tools

Decorate a typed function with `@tool`:

```python
from mycode import Agent, tool


@tool
def greet(name: str) -> str:
    """Return a friendly greeting.

    Args:
        name: Person name.
    """

    return f"hello, {name}"


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    tools=[greet],
)
```

To call a built-in tool from inside your own tool, type the first parameter as `ToolContext`:

```python
from mycode import ToolContext, tool


@tool
def summarize_file(ctx: ToolContext, path: str) -> str:
    """Return the first line of a text file."""

    result = ctx.read(path)
    return result.output.splitlines()[0] if result.output else ""
```

Async tools use `await ctx.aread()`, `await ctx.awrite()`, `await ctx.aedit()`, and `await ctx.abash()`. Use `await ctx.acall(name, args)` to call another registered tool by name.

## Tool hooks

Inspect or replace tool calls before they run. Return `None` from `before_tool` to let the tool execute, or a `ToolExecutionResult` to skip it:

```python
from mycode import Agent, Hooks, ToolExecutionResult, bash_tool

hooks = Hooks()


@hooks.before_tool
async def block_rm(ctx):
    cmd = str(ctx.tool_input.get("command") or "")
    if ctx.tool_name == "bash" and "rm -rf" in cmd:
        return ToolExecutionResult(output="error: blocked", is_error=True)
    return None


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    tools=[bash_tool],
    hooks=hooks,
)
```

`@hooks.after_tool` runs after the tool and can replace the result (audit, redact, etc.).

## Further reading

See [docs/sdk.md](https://github.com/legibet/mycode/blob/main/docs/sdk.md) for the streaming event API, cancellation, retries, compaction, session internals, and the full `Agent` / `@tool` reference.
