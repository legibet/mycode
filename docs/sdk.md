# SDK

Source: `mycode/src/mycode/`

## Package

`mycode-sdk` is a standalone Python SDK for multi-turn tool-calling agents. Import name: `mycode`. Ships independently of the CLI.

## Agent

```python
from mycode import Agent, bash_tool, read_tool

agent = Agent(
    model="claude-sonnet-4-6",
    api_key="YOUR_API_KEY",
    system="You are helpful.",
    tools=[read_tool, bash_tool],   # default: no tools registered
)

async for event in agent.achat("Hello"):
    ...
```

`achat(user_input)` drives **one user turn** as a streaming async iterator. A turn repeats `provider → tool calls → provider` internally until the assistant stops calling tools; the iterator ends when that turn is done.

`user_input` is either a `str` or a `ConversationMessage` with `role="user"`. A passed message's `content` and `meta` are kept verbatim; attachments are appended to its `content`.

### Attachments

```python
from mycode import Agent, Attachment

agent.run(
    "Summarize these files.",
    attachments=[
        "notes.txt",
        Attachment.path("diagram.png"),
        Attachment.bytes(pdf_bytes, media_type="application/pdf", name="report.pdf"),
    ],
)
```

`attachments` accepts `str | Path | Attachment`; bare strings and `Path` are treated as `Attachment.path(...)`. Blocks are appended to the user message in the given order, persisted, and replayed on later turns:

- A path to PNG/JPEG/GIF/WebP becomes an `image` block.
- A path to PDF becomes a `document` block.
- A path to a UTF-8 text file becomes a `<file name="…">…</file>` text block with `meta.attachment=True`.
- `Attachment.bytes(...)` with `image/png`, `image/jpeg`, `image/gif`, or `image/webp` becomes an `image` block.
- `Attachment.bytes(..., media_type="application/pdf")` becomes a `document` block.
- `Attachment.text(data, name=...)` becomes the same wrapped text block.

Document attachments support `application/pdf` only.

Bad input — unknown path, directory, non-UTF-8 binary that isn't a recognized image or PDF, missing or unsupported `media_type` — raises `ValueError` before the provider is touched. An image or PDF on a model that doesn't advertise that capability yields the existing `error` event instead.

### `run()` — synchronous wrapper

`run()` is a thin wrapper around `achat()`. It consumes the stream via `asyncio.run`, concatenates the `text` deltas into `RunResult.text`, stashes every event in `RunResult.events`, and captures the first error message in `RunResult.error`:

```python
result = agent.run("Hello")
print(result.text)
```

Because it calls `asyncio.run`, `run()` must not be invoked from inside an already-running event loop — use `achat()` there. `run()` also discards the ability to observe individual events as they happen; reach for `achat()` whenever you need to render tool calls, reasoning, or partial text live.

### Multi-turn conversations

`agent.messages` accumulates across `achat()` and `run()` calls. Keep the same `Agent` instance and call either method again — the previous turn is already in memory, so the next call just extends it:

```python
agent = Agent(model="...", api_key="...")

async for _ in agent.achat("hi"):
    ...
async for _ in agent.achat("follow-up that references the earlier answer"):
    ...
```

`agent.clear()` drops the in-memory history without touching the on-disk log. `cwd` defaults to the current working directory and is the working directory for the built-in file and shell tools.

### Cancellation

`agent.cancel()` aborts the in-flight turn from another task: it sets the cancel flag, terminates active bash subprocesses, and cancels the provider stream. The active `achat()` yields one final `error` event with `message="cancelled"` and stops. Already streamed `thinking`/`text` blocks are kept in memory and persisted when session persistence is enabled. `run()` collects the same event into `RunResult.events` and copies its message into `RunResult.error`.

### Streaming events

`achat()` yields `Event(type, data)`:

| `type`           | `data`                                                                   |
| ---------------- | ------------------------------------------------------------------------ |
| `reasoning`      | `{"delta": str}`                                                         |
| `reasoning_done` | `{"duration_ms": int}`                                                   |
| `text`           | `{"delta": str}`                                                         |
| `tool_start`     | `{"tool_call": {"id", "name", "input"}}`                                 |
| `tool_output`    | `{"tool_use_id", "output"}` — only for tools with `streams_output=True`  |
| `tool_done`      | `{"tool_use_id", "output", "is_error", "metadata"?, "content"?}`         |
| `compact`        | `{}` — emitted right after a compact marker is appended                  |
| `error`          | `{"message"}` — fatal for the turn; the iterator stops after emitting it |

## Sessions

Persistence is opt-in. Without `session_dir` the agent runs purely in memory and touches no files. Pass a `session_dir` to turn persistence on:

```python
agent = Agent(
    model="claude-sonnet-4-6",
    api_key="...",
    session_dir=Path("/data/chats"),   # root directory for all sessions
    session_id="chat-42",              # subdirectory under session_dir
)
```

### What gets persisted

Every message emitted during a turn — the user input, the assistant response (including `thinking` blocks), each `tool_result`, plus inline `compact` and `rewind` markers — is appended as one JSONL line to `<session_dir>/<session_id>/messages.jsonl`. The SDK never rewrites or deletes past lines.

Runtime-only fields are **not** persisted: the `system` prompt, `api_key`, `api_base`, the registered `tools`, and per-turn `provider` / `model` (those travel as `meta` on the individual assistant message).

The session subdirectory is created lazily. Constructing an `Agent` with an unused `session_id` does not write anything — `<session_dir>/<session_id>/` and `messages.jsonl` only appear when the first message is persisted. The `session_dir` root itself is created on `Agent` construction.

### Resolving `session_dir` and `session_id`

| `session_dir` | `session_id`                     | behaviour                                                        |
| ------------- | -------------------------------- | ---------------------------------------------------------------- |
| `None`        | any                              | no persistence; a runtime-only uuid is assigned when omitted     |
| `Path(...)`   | `None`                           | persistence on; a fresh uuid is allocated                        |
| `Path(...)`   | `"X"`, `<dir>/X/` does not exist | new session; subdirectory created on the first persisted message |
| `Path(...)`   | `"X"`, `<dir>/X/` exists         | history auto-loaded into `agent.messages` during `__init__`      |

Resuming across processes is therefore implicit: construct an `Agent` with the same `(session_dir, session_id)` and the conversation is back. If you also pass `messages=[]` or `messages=[...]` while the session already exists on disk, `__init__` refuses with `ValueError` — there is no "force fresh" shortcut, because the empty list would split-brain the JSONL log. Delete the session (`SessionStore.delete_session`) or pick a different `session_id` instead.

### `on_persist`

`achat(..., on_persist=coro)` and `run(..., on_persist=coro)` await `coro(message)` once per persisted message, **before** the internal store appends it. It fires for the user input, the assistant response, `tool_result` messages, and `compact` events alike, and works with or without `session_dir`. Use it as a custom persistence backend, or to stage related records alongside the SDK's own append (the CLI web server lands rewind markers this way).

### Compaction

When a turn reaches a full assistant/tool-result boundary the agent compares the latest assistant message's `meta.total_tokens` against `context_window * compact_threshold` (default `0.8`; pass `0` to disable). If over, it asks the same provider/model for a text-only summary capped at `max_tokens=8192`, persists a `compact` marker, and appends it inline to `agent.messages`. The pre-compact messages stay in place; only the next provider request sees the summary substitution.

Compaction is best-effort: a failed summary call is logged and the turn continues with the uncompacted history. The exception is a user-initiated cancel inside the summary call, which ends the turn with `error` `message="cancelled"`.

See `docs/sessions.md` for the on-disk record format, the projection rule that builds the provider-facing view, and the replay rules applied by `SessionStore.load_session`.

## Tools

### Built-ins

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

Four built-in tools, opted in via `tools=[...]`. Only `bash_tool` streams incremental output as `tool_output` events; the other three return a single `tool_done` result. Cancelled streaming tools return emitted output followed by `error: cancelled`.

### `@tool`

`@tool` wraps a sync or `async def` function as a `ToolSpec`. Parameters are validated with Pydantic and exported as the provider JSON schema.

```python
from mycode import tool


@tool
def greet(name: str) -> str:
    """Return a friendly greeting.

    Args:
        name: Person name.
    """

    return f"hello, {name}"
```

Tool names and descriptions:

- The function name becomes the tool name. Use `@tool(name=...)` to set a different provider-facing name.
- The docstring summary becomes the tool description. Use `@tool(description=...)` to set it explicitly.
- Google-style `Args:` entries describe top-level parameters. Use `@tool(parameters={...})` to set those descriptions explicitly.
- Pydantic `Field(description=...)` describes fields inside nested models.

Unknown `parameters={...}` keys raise at decoration time.

Explicit metadata keeps schema text close to the decorator:

```python
@tool(
    name="lookup",
    description="Find entries by key.",
    parameters={"key": "Lookup key.", "limit": "Maximum results."},
)
def lookup_entries(key: str, limit: int = 10) -> list[str]:
    ...
```

Use Pydantic models for nested input:

```python
from pydantic import BaseModel, Field


class Replacement(BaseModel):
    old_text: str = Field(alias="oldText", description="Exact text to find.")
    new_text: str = Field(alias="newText", description="Replacement text.")


@tool(parameters={"path": "File path.", "edits": "Replacement entries."})
def replace(path: str, edits: list[Replacement]) -> str:
    """Replace text snippets."""

    ...
```

Async tools work the same way:

```python
@tool
async def fetch_url(url: str) -> str:
    """Fetch a URL and return its body."""

    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text
```

A bare `str` return becomes the tool output replayed to the provider. Other JSON-serializable returns are dumped to JSON. Return `ToolExecutionResult` when you need `content`, `metadata`, or `is_error`.

### `ToolContext`

Annotate the first parameter of a custom tool as `ToolContext` to have the runtime context injected. The two things you typically reach for:

- `ctx.read` / `ctx.write` / `ctx.edit` / `ctx.bash` — typed facades over the built-ins, useful when your tool wraps or composes built-in behaviour. `ctx.call(name, args)` is the generic by-name dispatch for any registered tool.
- `ctx.emit(line)` — emit one `tool_output` event. Only meaningful on specs declared with `streams_output=True`.

`ctx.tool_output_dir` is always a valid `Path`: `<session_dir>/<session_id>/tool-output/` when the agent has a session, or a tempdir-scoped fallback otherwise. The built-in `bash` tool spills large outputs there lazily; custom tools can treat it as their own scratch area.

### Tool hooks

`Hooks` lets SDK callers observe or replace model-requested tool executions without changing the provider protocol or message format:

```python
from mycode import Agent, Hooks, ToolExecutionResult, bash_tool

hooks = Hooks()


@hooks.before_tool
async def block_dangerous_bash(ctx):
    if ctx.tool_name == "bash" and "rm -rf" in str(ctx.tool_input.get("command") or ""):
        return ToolExecutionResult(output="error: blocked by hook", is_error=True)
    return None


@hooks.after_tool
async def audit(_ctx, _result):
    return None


agent = Agent(model="...", api_key="...", tools=[bash_tool], hooks=hooks)
```

`ToolHookContext` carries `session_id`, `cwd`, `provider`, `model`, `tool_call_id`, `tool_name`, `tool_input`, and `tool` (the `ToolSpec`). `tool_input` is recursively frozen — nested dicts become `MappingProxyType`, lists become tuples — so hooks cannot mutate what the UI shows or the tool receives.

- `before_tool(ctx)` hooks run in registration order. Returning `None` continues; returning a `ToolExecutionResult` skips the real tool and uses that result.
- `after_tool(ctx, result)` hooks run in registration order for both real and skipped results. Returning `None` keeps the current result; returning a `ToolExecutionResult` replaces it for later hooks and the final `tool_done` event.
- `tool_start` is emitted before `before_tool` runs, so a hook that blocks (e.g. waiting on an external review) keeps the call visible in the event stream while it waits.
- `before_tool` exceptions become `ToolExecutionResult(output="error: tool hook failed: ...", is_error=True)`, the real tool is not run, and later `before_tool` hooks are skipped.
- `after_tool` exceptions are logged and the existing tool result is forwarded unchanged. Later `after_tool` hooks are skipped. Hooks that need to fail closed (e.g. redaction) must catch internally and return an explicit error result.
- Cancellation is controlled by the runtime. Cancelled tool results do not run `after_tool` hooks and cannot be replaced.
- Streaming tools still stream `tool_output` during real execution. If a `before_tool` hook skips the tool, no live `tool_output` events are emitted.

### `cancel_all_tools()`

Terminates every bash subprocess started by any `ToolExecutor` in the process. `agent.cancel()` already covers its own executor; use this for signal handlers or shutdown hooks that don't hold an agent reference.
