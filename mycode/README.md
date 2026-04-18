# mycode-sdk

Lightweight Python SDK for the mycode multi-turn tool-calling agent. Import name: `mycode`.

## Install

```bash
uv add mycode-sdk
# or
pip install mycode-sdk
```

## Quick Start

`Agent(...)` fills in sensible defaults: provider inferred from the model id, `session_id` generated, session log auto-persisted to `~/.mycode/sessions/<session_id>/`. By default no tools are registered — pick the built-ins you want or register your own via `@tool`.

```python
import asyncio

from mycode import Agent, bash_tool, read_tool


async def main() -> None:
    agent = Agent(
        model="claude-sonnet-4-6",
        api_key="YOUR_API_KEY",
        cwd=".",
        system="You are a concise coding assistant.",
        tools=[read_tool, bash_tool],
    )

    async for event in agent.achat("Read pyproject.toml and tell me the project name."):
        if event.type == "text":
            print(event.data["delta"], end="")


asyncio.run(main())
```

To resume a previous conversation, pass the same `session_id` (the agent loads its own history from disk).

## Built-in tools

Pick and combine the four bundled coding tools:

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

## Custom Tools

`@tool` wraps a plain Python function (sync or async) as a `ToolSpec`. If the first parameter is annotated `ToolContext`, the context is injected; use `ctx.call("read", {...})` to invoke another registered tool.

```python
from mycode import Agent, ToolContext, read_tool, tool


@tool
def summarize_file(ctx: ToolContext, path: str) -> str:
    """Return the first line of a text file."""

    result = ctx.call("read", {"path": path})
    return result.model_text.splitlines()[0] if result.model_text else ""


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="YOUR_API_KEY",
    cwd=".",
    tools=[read_tool, summarize_file],
)
```

Type hints drive the JSON schema. Unknown types raise; missing docstrings raise. `async def` tools are run via `asyncio.run` on the executor's worker thread.
