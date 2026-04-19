# mycode-sdk

Lightweight Python SDK for building multi-turn tool-calling agents. Import name: `mycode`.

## Install

```bash
uv add mycode-sdk
# or
pip install mycode-sdk
```

## Quick start

`Agent(...)` fills in sensible defaults: provider inferred from the model id, `session_id` generated, no persistence unless you pass `session_dir=`. No tools are registered by default — opt in via `tools=[...]`.

```python
import asyncio

from mycode import Agent, bash_tool, read_tool


async def main() -> None:
    agent = Agent(
        model="claude-sonnet-4-6",
        api_key="YOUR_API_KEY",
        cwd=".",
        tools=[read_tool, bash_tool],
    )

    async for event in agent.achat("Read pyproject.toml and tell me the project name."):
        if event.type == "text":
            print(event.data["delta"], end="", flush=True)


asyncio.run(main())
```

Call `achat` again on the same `Agent` to continue the conversation — history accumulates in `agent.messages`. To persist across processes, pass a `session_dir` (the root directory under which each `session_id` is a subdirectory); reconstruct with the same `(session_dir, session_id)` to resume.

## Built-in tools

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

## Custom tools

`@tool` wraps a sync or async Python function as a `ToolSpec`. If the first parameter is annotated `ToolContext`, the context is injected; use `ctx.read / ctx.write / ctx.edit / ctx.bash` to invoke the built-ins, or `ctx.call(name, args)` for any registered tool by name.

```python
from mycode import Agent, ToolContext, read_tool, tool


@tool
def summarize_file(ctx: ToolContext, path: str) -> str:
    """Return the first line of a text file."""

    result = ctx.read(path)
    return result.output.splitlines()[0] if result.output else ""


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="YOUR_API_KEY",
    cwd=".",
    tools=[read_tool, summarize_file],
)
```

See `docs/sdk.md` for multi-turn behaviour, session persistence, and the full `Agent` / `@tool` reference.
