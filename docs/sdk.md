# SDK

Source: `mycode/src/mycode/`

## Package

`mycode-sdk` (import name `mycode`) is the public Python package for embedding the mycode agent loop. The runtime is the package — there is no separate factory or wrapper class.

## Public exports

- `Agent`, `Event`, `PersistCallback`
- `ToolExecutor`, `ToolSpec`, `ToolContext`, `ToolExecutionResult`
- `DEFAULT_TOOL_SPECS`, `read_tool`, `write_tool`, `edit_tool`, `bash_tool`, `cancel_all_tools`
- `SessionStore`
- `tool`
- Message helpers: `ContentBlock`, `ConversationMessage`, `build_message`, `text_block`, `image_block`, `document_block`, `thinking_block`, `tool_use_block`, `tool_result_block`, `user_text_message`, `assistant_message`, `flatten_message_text`

## Agent

```python
from mycode import Agent, bash_tool, read_tool

agent = Agent(
    model="kimi-k2.5",
    api_key="YOUR_API_KEY",
    api_base="https://api.moonshot.cn/anthropic",
    cwd=".",
    session_id="my-session",      # omit for a fresh uuid
    system="You are helpful.",
    tools=[read_tool, bash_tool], # default: no tools
)

async for event in agent.achat("Hello"):
    ...
```

Defaults filled in by `Agent.__init__`:

1. `provider` — inferred from the model id prefix (`gpt-` → `openai`, `claude-` → `anthropic`, `gemini-` → `google`, …). Raises `ValueError` if the prefix is not recognized.
2. `session_id` — uuid hex if not given.
3. `session_dir` — `~/.mycode/sessions/<session_id>/` (or the `MYCODE_HOME` override).
4. `messages` — auto-loaded from the session log if the session already exists on disk.
5. `supports_image_input` / `supports_pdf_input` — looked up in the bundled model catalog.

Every emitted message is appended to the session log automatically. The CLI / web server pass their own `on_persist` to insert side effects (the server uses it to land a rewind marker before the next user message).

## Tools

### Built-ins

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

`ToolSpec` instances — pass them through `tools=[...]`. Nothing is registered unless you ask.

### @tool

```python
from mycode import ToolContext, tool


@tool
def greet(name: str) -> str:
    """Return a friendly greeting."""

    return f"hello, {name}"


@tool
async def fetch_url(url: str) -> str:
    """Fetch a URL and return its body."""

    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text


@tool(streams_output=True)
def tail(ctx: ToolContext, path: str) -> str:
    """Stream the tail of a file."""

    for line in open(path):
        if ctx.emit:
            ctx.emit(line.rstrip("\n"))
    return "done"
```

- Sync and async functions are both supported. Async tools are run via `asyncio.run` on the executor's worker thread.
- If the first parameter is annotated `ToolContext`, it is injected automatically.
- Remaining parameters drive the JSON schema sent to the provider. Unknown / unannotated types raise `TypeError`.
- Missing docstrings raise `ValueError` — pass `description=...` explicitly if no docstring fits.
- A plain `str` return becomes `ToolExecutionResult(model_text=..., display_text=...)`; return a `ToolExecutionResult` directly for custom `display_text`, `is_error`, or structured `content`.

### ToolContext

Passed to every tool runner. Exposes:

- `cwd`, `session_dir`, `tool_output_dir`, `supports_image_input`
- `tool_call_id`, `emit`
- `executor` — the owning `ToolExecutor`
- `call(name, args)` — invoke another registered tool. Forwards `tool_call_id` and `emit` so a streaming tool that delegates to `bash` keeps producing `tool_output` events upstream.

## Sessions

`SessionStore` reads and writes the on-disk JSONL format under `~/.mycode/sessions/<session_id>/`. `Agent` constructs its own internal store rooted at `session_dir.parent` and appends each message there. Callers that already manage persistence (CLI / web server) pass their own `on_persist` for additional side effects; Agent still appends.

## Code Map

- `mycode/src/mycode/__init__.py` — public re-exports
- `mycode/src/mycode/agent.py` — agent loop
- `mycode/src/mycode/tools.py` — `ToolSpec`, `ToolExecutor`, `ToolContext`, built-in tool implementations, `@tool` decorator
- `mycode/src/mycode/session.py` — `SessionStore`
- `tests/test_public_api.py` — public API tests
