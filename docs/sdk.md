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

`agent.cancel()` aborts the in-flight turn from another task: it sets the cancel flag, terminates active bash subprocesses, and cancels the provider stream. The active `achat()` yields one final `error` event with `message="cancelled"` and stops. `run()` collects the same event into `RunResult.events` and copies its message into `RunResult.error`.

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
| `compact`        | `{"message", "compacted_count"}`                                         |
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

Every message emitted during a turn — the user input, the assistant response (including `thinking` blocks), each `tool_result`, and any `compact` event — is appended as one JSONL line to `<session_dir>/<session_id>/messages.jsonl`. The SDK never rewrites or deletes past lines; compaction records a new event instead of mutating history.

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

When a turn completes the agent checks the last assistant message's `usage.input_tokens` (as reported by the provider). If that count has reached `context_window * compact_threshold`, it asks the same provider for a summary, appends a `compact` event to the log, and rebuilds `agent.messages` from the summary so the next turn sees a shorter history. The default threshold is `0.8`; pass `compact_threshold=0` to disable.

If compaction itself fails — provider error, cancellation, empty summary — the agent logs a warning and continues with the uncompacted history. The turn that triggered it does not fail.

See `docs/sessions.md` for the on-disk record format and the replay rules (`compact` → `rewind`) applied by `SessionStore.load_session`. The loader is a pure reader; provider adapters close orphan `tool_use` blocks at replay time.

## Tools

### Built-ins

```python
from mycode import read_tool, write_tool, edit_tool, bash_tool
```

Four built-in tools, opted in via `tools=[...]`. Only `bash_tool` streams incremental output as `tool_output` events; the other three return a single `tool_done` result.

### `@tool`

`@tool` wraps a sync or `async def` Python function as a `ToolSpec`. Parameter type hints drive the JSON schema sent to the provider; the docstring becomes the tool description.

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

- Missing docstrings raise at decoration time — pass `description=...` when no docstring fits.
- Async tools are dispatched via `asyncio.run` on the executor's worker thread, so each call gets its own fresh event loop.
- A bare `str` return becomes the tool's `output` (replayed to the provider on the next turn). Any other JSON-serializable return is dumped to JSON first. Return a `ToolExecutionResult` for finer control: `output` (replayed), `content` (multimodal blocks such as images, also replayed), `metadata` (structured UI data; `edit` uses this to carry a unified patch and line stats), and `is_error`.

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
