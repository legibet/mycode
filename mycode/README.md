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
from pathlib import Path

from mycode import Agent, tool


@tool
def read_file(path: str) -> str:
    """Read a UTF-8 text file.

    Args:
        path: File path, relative to the working directory.
    """

    return Path(path).read_text(encoding="utf-8")


async def main() -> None:
    agent = Agent(
        model="claude-sonnet-4-6",
        api_key="YOUR_API_KEY",
        tools=[read_file],
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

## Tools

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

To call another registered tool from inside a tool, type the first parameter as `ToolContext` and dispatch by name:

```python
from mycode import ToolContext, tool


@tool
def greet_team(ctx: ToolContext, names: list[str]) -> str:
    """Greet several people at once."""

    return "\n".join(ctx.call("greet", {"name": name}).output for name in names)
```

Async tools use `await ctx.acall(name, args)` instead.

## Tool hooks

Inspect or replace tool calls before they run. Return `None` from `before_tool` to let the tool execute, or a `ToolExecutionResult` to skip it:

```python
import os

from mycode import Agent, Hooks, ToolExecutionResult, tool

hooks = Hooks()


@tool
def delete_file(path: str) -> str:
    """Delete a file.

    Args:
        path: Path of the file to delete.
    """

    os.remove(path)
    return f"deleted {path}"


@hooks.before_tool
async def protect_dotfiles(ctx):
    if ctx.tool_name == "delete_file" and str(ctx.tool_input.get("path") or "").startswith("."):
        return ToolExecutionResult(output="error: blocked", is_error=True)
    return None


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    tools=[delete_file],
    hooks=hooks,
)
```

`@hooks.after_tool` runs after the tool and can replace the result (audit, redact, etc.).

## Further reading

See [docs/sdk.md](https://github.com/legibet/mycode/blob/main/docs/sdk.md) for the streaming event API, cancellation, retries, compaction, session internals, and the full `Agent` / `@tool` reference.
