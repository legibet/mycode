# SDK

Source: `mycode/src/mycode/`

## Package

`mycode-sdk` is a standalone Python SDK for multi-turn tool-calling agents. Import name: `mycode`. Ships independently of the CLI.

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
    model="claude-sonnet-4-6",
    api_key="YOUR_API_KEY",
    cwd=".",
    system="You are helpful.",
    tools=[read_tool, bash_tool],   # default: no tools registered
)

async for event in agent.achat("Hello"):
    ...
```

One `achat(user_input)` call drives **one user turn**: the agent records `user_input`, asks the provider for an assistant message, runs any tool calls locally, appends one user-side `tool_result` message, and loops until the assistant stops calling tools. Streaming events are yielded in order along the way.

### Multi-turn conversation

`agent.messages` accumulates across `achat` calls. Keep the same `Agent` instance and call `achat` again to continue the conversation — the previous turn is already in memory, so the next call just extends it:

```python
agent = Agent(model="...", api_key="...", cwd=".")

async for _ in agent.achat("hi"):
    ...
async for _ in agent.achat("follow-up that references the earlier answer"):
    ...
```

`agent.clear()` drops the in-memory history without touching the on-disk log. To resume across processes, pass the same `session_dir` and `session_id` (see below).

### Cancellation

`agent.cancel()` aborts the in-flight turn from another task: sets the cancel flag, terminates active bash subprocesses, and cancels the provider stream. The active `achat` yields an `error` event with `message="cancelled"` and returns.

### Streaming events

`achat` yields `Event(type, data)`:

| `type`        | `data`                                                                   |
| ------------- | ------------------------------------------------------------------------ |
| `reasoning`   | `{"delta": str}`                                                         |
| `text`        | `{"delta": str}`                                                         |
| `tool_start`  | `{"tool_call": {"id", "name", "input"}}`                                 |
| `tool_output` | `{"tool_use_id", "output"}` — only for tools with `streams_output=True`  |
| `tool_done`   | `{"tool_use_id", "output", "is_error", "metadata"?, "content"?}`         |
| `compact`     | `{"message", "compacted_count"}`                                         |
| `error`       | `{"message"}` — fatal for the turn; the iterator stops after emitting it |

## Sessions

Persistence is opt-in. With no `session_dir`, `achat` runs in memory only and touches no files. Pass a `session_dir` to enable persistence:

```python
agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    cwd=".",
    session_dir=Path("/data/chats"),   # root directory for all sessions
    session_id="chat-42",              # subdirectory under session_dir
)
```

Rules:

- `session_dir=None` — no persistence. `session_id` still gets a uuid for runtime tagging.
- `session_dir=Path(...)`, `session_id=None` — persistence; a uuid is allocated.
- `session_dir=Path(...)`, `session_id="X"` — persistence at `<session_dir>/X/`. If that session already exists, history is auto-loaded into `agent.messages` during `__init__`. Passing `messages=[]` or `messages=[...]` when the session exists is refused with `ValueError` (would split-brain the on-disk JSONL).

`achat(..., on_persist=coro)` awaits `coro(message)` right before each append. Works with or without `session_dir` — use it to plug in a custom persistence backend or to stage related records (the web server lands rewind markers this way).

See `docs/sessions.md` for the on-disk record format and compact / rewind semantics.

## Tools

### Built-ins

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

Opt in via `tools=[...]`. Only `bash_tool` streams incremental output (emitted as `tool_output` events); the other three return a single result.

### @tool

`@tool` wraps a sync or `async def` Python function as a `ToolSpec`. Parameter type hints drive the JSON schema sent to the provider.

```python
from mycode import ToolContext, ToolExecutionResult, tool


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
def tail(ctx: ToolContext, path: str) -> ToolExecutionResult:
    """Stream a file line by line."""

    for line in open(path):
        if ctx.emit:
            ctx.emit(line.rstrip("\n"))
    return ToolExecutionResult(output="done")
```

- A missing docstring raises at decoration time — pass `description=...` if no docstring fits.
- Async tools run via `asyncio.run` on the executor's worker thread, so each call gets its own fresh event loop.
- Return a `ToolExecutionResult` to set `output` (replayed to the provider), `content` (multimodal blocks such as images, also replayed), `metadata` (UI-side structured data — e.g. `edit` uses this to carry per-edit line numbers), and `is_error` independently. A bare `str` is used as `output`; any other JSON-serializable return is dumped to JSON first.

### ToolContext

Injected when the first parameter is typed `ToolContext`; built-in tools receive one too. Fields:

- `cwd`, `tool_output_dir`, `supports_image_input`, `tool_call_id`, `emit`.

Methods custom tools typically use:

- `ctx.read(path, *, offset=None, limit=None)`, `ctx.write(path, content)`, `ctx.edit(path, edits)`, `ctx.bash(command, *, timeout=None)` — typed facades for the built-ins. Return a `ToolExecutionResult`.
- `ctx.call(name, args)` — generic dispatch by name, for custom tools that wrap another registered tool.
- `ctx.emit(line)` — stream one line as a `tool_output` event (only meaningful when the spec has `streams_output=True`).

`tool_output_dir` is always a valid `Path`: `<session_dir>/<session_id>/tool-output/` when the agent has a session, or a tempdir-scoped equivalent when it doesn't. Built-ins (bash in particular) spill large outputs there lazily; custom tools can treat it as their own scratch area.

### cancel_all_tools()

Terminates every bash subprocess started by any `ToolExecutor` in the process. `agent.cancel()` already covers its own executor; use this for signal handlers or shutdown hooks that don't hold an agent reference.
