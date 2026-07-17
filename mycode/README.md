# mycode-sdk

Lightweight Python SDK for building AI agents.

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

For a simple synchronous call, use `run()`:

```python
result = agent.run("Read pyproject.toml and tell me the project name.")
print(result.text)
```

## Multi-turn conversations

Call `achat()` or `run()` again on the same `Agent` to continue the conversation:

```python
agent = Agent(model="claude-sonnet-4-6", api_key="...")

agent.run("What is 2 + 2?")
agent.run("Now multiply that by 10.")    # remembers the earlier answer
```

## Attachments

Pass `attachments` to `achat()` or `run()` to add files alongside the prompt:

```python
from mycode import Attachment

agent.run("Describe these.", attachments=["diagram.png", "report.pdf", "notes.txt"])

# Or build them explicitly — raw bytes and inline text never touch the disk:
agent.run(
    "Review.",
    attachments=[
        Attachment.path("diagram.png"),
        Attachment.bytes(png_data, media_type="image/png"),
        Attachment.text("TODO: ship it", name="note.md"),
    ],
)
```

Images support `image/png`, `image/jpeg`, `image/gif`, `image/webp`; documents support `application/pdf`. Sending an image or PDF to a model that lacks that capability yields an `error` event; a bad path or unsupported type raises `ValueError` before the model is called.

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

See [docs/sdk.md](../docs/sdk.md) for the event stream, cancellation, sessions, and the full `Agent` / `@tool` reference.
