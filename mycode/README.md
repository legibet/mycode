# mycode-sdk

Lightweight Python SDK for building agents.

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

`Agent(...)` infers the provider from the model id and defaults `cwd` to the current working directory. No tools are registered unless you pass `tools=[...]`, and nothing is persisted unless you pass `session_dir=`.

For a synchronous call, use `run()` — it collects the stream into a `RunResult`:

```python
result = agent.run("Read pyproject.toml and tell me the project name.")
print(result.text)
```

Call `achat` or `run` again on the same `Agent` to continue the conversation — history accumulates in `agent.messages`.

To persist across processes, pass `session_dir` as the root directory; each `session_id` becomes a subdirectory. Reconstruct with the same `(session_dir, session_id)` to resume.

## Built-in tools

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

Only `bash_tool` streams incremental output as `tool_output` events; the others return a single result.

## Custom tools

`@tool` wraps a sync or `async def` Python function as a `ToolSpec`. Parameter type hints become the JSON schema sent to the provider.

Annotate the first parameter as `ToolContext` to have the context injected. Use `ctx.read / ctx.write / ctx.edit / ctx.bash` to invoke the built-ins, or `ctx.call(name, args)` for any registered tool by name.

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
    tools=[read_tool, summarize_file],
)
```

A bare `str` return becomes the tool `output`; any other JSON-serializable value is dumped to JSON. For finer control, return a `ToolExecutionResult` to set `output`, `content` (multimodal blocks such as images), `metadata` (structured UI data), and `is_error` independently.

See [docs/sdk.md](../docs/sdk.md) for the event stream, cancellation, session rules, and the full `Agent` / `@tool` reference.
