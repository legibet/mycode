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

Call `achat()` or `run()` again on the same `Agent` — history accumulates automatically:

```python
agent = Agent(model="claude-sonnet-4-6", api_key="...")

agent.run("What is 2 + 2?")
agent.run("Now multiply that by 10.")    # remembers the earlier answer
```

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

Construct another `Agent` with the same `(session_dir, session_id)` later to resume the conversation — the history is loaded automatically.

## Built-in tools

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

Four tools for reading, writing, editing files, and running shell commands. Opt in by passing them to `tools=[...]`.

## Custom tools

Decorate any function with `@tool`. Parameter type hints become the JSON schema sent to the provider; the docstring becomes the description:

```python
from mycode import Agent, tool


@tool
def greet(name: str) -> str:
    """Return a friendly greeting."""

    return f"hello, {name}"


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    tools=[greet],
)
```

To call a built-in tool from inside your own, type the first parameter as `ToolContext`:

```python
from mycode import ToolContext, tool


@tool
def summarize_file(ctx: ToolContext, path: str) -> str:
    """Return the first line of a text file."""

    result = ctx.read(path)
    return result.output.splitlines()[0] if result.output else ""
```

See [docs/sdk.md](../docs/sdk.md) for the event stream, cancellation, sessions, and the full `Agent` / `@tool` reference.
