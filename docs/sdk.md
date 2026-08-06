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

An unknown path, directory, unsupported binary, or missing or unsupported `media_type` raises `ValueError` before the provider is called. An image or PDF on a model that does not advertise that capability yields an `error` event.

### `run()` synchronous wrapper

`run()` is a thin wrapper around `achat()`. It consumes the stream via `asyncio.run`, concatenates the `text` deltas into `RunResult.text`, stashes every event in `RunResult.events`, captures the first error message in `RunResult.error`, and keeps the last `usage` event's payload as `RunResult.usage` (the whole turn's final cumulative usage and cost):

```python
result = agent.run("Hello")
print(result.text)
```

Because it calls `asyncio.run`, `run()` must not be invoked from inside an active event loop. Use `achat()` there. `run()` also hides individual streaming events; use `achat()` to render tool calls, reasoning, or partial text live.

### Multi-turn conversations

`agent.messages` accumulates across `achat()` and `run()` calls. Calling either method again on the same `Agent` extends the existing conversation:

```python
agent = Agent(model="...", api_key="...")

async for _ in agent.achat("hi"):
    ...
async for _ in agent.achat("follow-up that references the earlier answer"):
    ...
```

`agent.clear()` drops the in-memory history without touching the on-disk log. `cwd` defaults to the current working directory and is the working directory for the built-in file and shell tools.

### Reasoning effort

`Agent(reasoning_effort=...)` accepts any string without consulting model metadata and passes it to the selected provider adapter. Adapters project the value into their protocol and may reject values that protocol does not represent, such as an unknown Gemini `ThinkingLevel`. Pass `None` to omit an explicit effort and use the provider default.

`ModelMetadata.reasoning_efforts` exposes the string efforts published in the bundled model catalog. It is informational and does not gate `Agent.reasoning_effort`: `None` means the catalog has no effort metadata, an empty tuple means it has no string effort, and a non-empty tuple contains the advertised values.

### Cancellation

`agent.cancel()` can be called from another task or thread. It sets the cancel flag, terminates active bash subprocesses, and cancels the active provider stream or async tool. Async tools receive `asyncio.CancelledError`. A cancelled tool emits an error `tool_done`; a cancelled provider stream emits an `error` event with `message="cancelled"`. Already streamed `thinking` and text are persisted when session persistence is enabled.

A synchronous function already running in a worker thread continues until it returns. Subprocesses started by `bash_tool` are terminated.

### Timeouts and retries

```python
Agent(
    request_timeout=300,        # transport timeout per provider attempt, seconds
    stream_start_timeout=60,    # max wait for the first upstream stream event
    max_retries=2,              # retries after the initial attempt
)
```

Validation: `request_timeout > 0`, `stream_start_timeout > 0`, `max_retries >= 0`. There is no disable semantics.

`stream_start_timeout` bounds everything up to the first event or chunk exposed by the upstream SDK: DNS, connect, request upload, response headers, and the wait for the initial lifecycle event or content chunk. After the stream has started, only the transport `request_timeout` applies; a stream that keeps sending heartbeats without model output has no automatic deadline and ends only via `cancel()`.

The Agent owns retries; provider SDK retries are disabled. Before output reaches the caller, it retries connection errors, timeouts, stream-start expiry, HTTP 408/409/429/5xx, and transient stream failures identified by the provider adapter. Once reasoning or text has been emitted, or a complete assistant message has been formed, failures surface as an `error` event instead; already-streamed reasoning/text is persisted with `meta.stop_reason="error"` (excluded from replay). Partial, unexposed tool-call arguments do not block a retry. User cancellation is never retried, and `cancel()` interrupts a backoff wait immediately.

Backoff is exponential with jitter (0.5s initial, ×2, capped at 8s); a positive `Retry-After` header up to 60s takes precedence. Each retry emits a `retry` event carrying `attempt` (the next 1-based attempt), `max_attempts` (`max_retries + 1`), `delay_seconds`, `reason` (`connection_error` / `request_timeout` / `stream_start_timeout` / `http_status` / `provider_error`), `message`, and `status_code` when applicable. After any failed attempt, the turn's cumulative `turn_usage` and `cost_usd` become `None` and stay unknown even when a later attempt succeeds; a retried compaction likewise drops `usage` from its compact marker.

Adapters raise `ProviderError` (`reason`, `retryable`, `status_code`, `retry_after`); a stream-start expiry that exhausts its retries raises `StreamStartTimeoutError`, a subclass distinct from cancellation. Both are exported from `mycode`.

### Streaming events

`achat()` yields `Event(type, data)`:

| `type`           | `data`                                                                   |
| ---------------- | ------------------------------------------------------------------------ |
| `reasoning`      | `{"delta": str}`                                                         |
| `reasoning_done` | `{"duration_ms": int}`                                                   |
| `text`           | `{"delta": str}`                                                         |
| `tool_start`     | `{"tool_call": {"id", "name", "input"}}`                                 |
| `tool_output`    | `{"tool_use_id", "output"}`; only for tools with `streams_output=True`   |
| `tool_done`      | `{"tool_use_id", "output", "is_error", "metadata"?, "content"?}`         |
| `compact`        | `{}`; emitted right after a compact marker is appended                   |
| `retry`          | fields under Timeouts and retries; emitted before each new attempt       |
| `usage`          | see below; emitted after every provider request                          |
| `error`          | `{"message"}`; fatal for the turn, then the iterator stops               |

### Usage and cost

A `usage` event follows each provider request, including automatic compaction. It reports the latest context occupancy and cumulative billing for the turn:

```python
{
    "context_tokens": 1456,        # latest normal request's usage.total_tokens
    "context_window": 200000,
    "model": "...",
    "provider": "...",
    "turn_usage": {                # cumulative for the turn
        "total_tokens": 2912, "input_tokens": 2800,
        "cache_read_tokens": None, "cache_write_tokens": None,
        "output_tokens": 112, "reasoning_tokens": None,
    },
    "cost_usd": 0.0123,
}
```

- `context_tokens` is the latest normal request's context usage. It is not cumulative.
- `turn_usage` and `cost_usd` accumulate all provider requests in the turn.
- If any request has unknown usage or cost, the affected cumulative values become `None`.
- A failed or cancelled request emits this unknown state before the turn stops or continues.

Per-request facts are persisted in `meta.usage`; see docs/sessions.md. `estimate_cost(usage, cost)` uses `ModelMetadata.cost` prices from models.dev, applies long-context tiers per request, and prefers an upstream-reported cost. It returns `None` when the available usage or prices are insufficient. OpenRouter suffix fallback metadata never supplies prices.

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

Every message emitted during a turn is appended as one JSONL line to `<session_dir>/<session_id>/messages.jsonl`. This includes user input, assistant responses, `thinking` blocks, each `tool_result`, and inline `compact` and `rewind` markers. The SDK never rewrites or deletes past lines.

Runtime-only fields are **not** persisted: the `system` prompt, `api_key`, `api_base`, the registered `tools`, and per-turn `provider` / `model` (those travel as `meta` on the individual assistant message).

The session subdirectory is created lazily. Constructing an `Agent` with an unused `session_id` does not write anything. `<session_dir>/<session_id>/` and `messages.jsonl` appear when the first message is persisted. The `session_dir` root is created on `Agent` construction.

### Resolving `session_dir` and `session_id`

| `session_dir` | `session_id`                     | behaviour                                                        |
| ------------- | -------------------------------- | ---------------------------------------------------------------- |
| `None`        | any                              | no persistence; a runtime-only uuid is assigned when omitted     |
| `Path(...)`   | `None`                           | persistence on; a fresh uuid is allocated                        |
| `Path(...)`   | `"X"`, `<dir>/X/` does not exist | new session; subdirectory created on the first persisted message |
| `Path(...)`   | `"X"`, `<dir>/X/` exists         | history auto-loaded into `agent.messages` during `__init__`      |

Construct an `Agent` with the same `(session_dir, session_id)` to resume across processes. Passing `messages=[]` or `messages=[...]` for an existing session raises `ValueError` because it would conflict with the JSONL log. Delete the session with `SessionStore.delete_session` or use a different `session_id`.

### `on_persist`

`achat(..., on_persist=coro)` and `run(..., on_persist=coro)` await `coro(message)` once per persisted message, **before** the internal store appends it. It fires for the user input, the assistant response, `tool_result` messages, and `compact` events alike, and works with or without `session_dir`. Use it as a custom persistence backend, or to stage related records alongside the SDK's own append (the CLI web server lands rewind markers this way).

### Compaction

When a turn reaches a full assistant/tool-result boundary the agent compares the latest assistant message's `meta.usage.total_tokens` against `context_window * compact_threshold` (default `0.8`; pass `0` to disable). If over, it asks the same provider/model for a text-only summary capped at `max_tokens=8192`, persists a `compact` marker, and appends it inline to `agent.messages`. The pre-compact messages stay in place; only the next provider request sees the summary substitution.

Compaction is best-effort: a failed summary call is logged and the turn continues with the uncompacted history. The exception is a user-initiated cancel inside the summary call, which ends the turn with `error` `message="cancelled"`.

#### Manual compaction

`await agent.acompact()` (and the synchronous `agent.compact()` wrapper) compacts on demand, independent of `compact_threshold`. Both run the same summary request and persistence path as automatic compaction and **return the persisted `compact` marker** (a `ConversationMessage`); they append no user or assistant turn. Pass `on_persist=coro` to stage the marker alongside your own store, exactly as `achat` does.

Manual compaction requires new context after the latest marker. Otherwise it raises `NothingToCompactError` before any provider request. A `cancel()` during the summary call raises `asyncio.CancelledError` and writes no marker. `compact()` raises `RuntimeError` inside a running event loop, matching `run()`.

```python
agent.run("Review the project")
marker = agent.compact()
agent.run("Continue with the next task")   # sends summary replay, not full history
```

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

The Agent awaits async tools on its event loop and runs sync tools in a worker thread. Use `ToolExecutor.aexecute()` from async code and `ToolExecutor.execute()` from sync code.

A bare `str` return becomes the tool output replayed to the provider. Other JSON-serializable returns are dumped to JSON. Return `ToolExecutionResult` when you need `content`, `metadata`, or `is_error`.

### `ToolContext`

Annotate the first parameter of a custom tool as `ToolContext` to have the runtime context injected:

- Sync tools use `ctx.read()`, `ctx.write()`, `ctx.edit()`, and `ctx.bash()`.
- Async tools use `await ctx.aread()`, `await ctx.awrite()`, `await ctx.aedit()`, and `await ctx.abash()`.
- `ctx.call(name, args)` and `await ctx.acall(name, args)` dispatch any registered tool by name.
- `ctx.emit(line)` emits one `tool_output` event. It only applies to specs declared with `streams_output=True`.

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

`ToolHookContext` carries `session_id`, `cwd`, `provider`, `model`, `tool_call_id`, `tool_name`, `tool_input`, and `tool` (the `ToolSpec`). `tool_input` is recursively frozen: nested dicts become `MappingProxyType` and lists become tuples. Hooks cannot mutate what the UI shows or the tool receives.

- `before_tool(ctx)` hooks run in registration order. Returning `None` continues; returning a `ToolExecutionResult` skips the real tool and uses that result.
- `after_tool(ctx, result)` hooks run in registration order for both real and skipped results. Returning `None` keeps the current result; returning a `ToolExecutionResult` replaces it for later hooks and the final `tool_done` event.
- `tool_start` is emitted before `before_tool` runs, so a hook that blocks (e.g. waiting on an external review) keeps the call visible in the event stream while it waits.
- `before_tool` exceptions become `ToolExecutionResult(output="error: tool hook failed: ...", is_error=True)`, the real tool is not run, and later `before_tool` hooks are skipped.
- `after_tool` exceptions are logged and the existing tool result is forwarded unchanged. Later `after_tool` hooks are skipped. Hooks that need to fail closed (e.g. redaction) must catch internally and return an explicit error result.
- Cancellation is controlled by the runtime. Cancelled tool results do not run `after_tool` hooks and cannot be replaced.
- Streaming tools still stream `tool_output` during real execution. If a `before_tool` hook skips the tool, no live `tool_output` events are emitted.

### `cancel_all_tools()`

Terminates every bash subprocess started by any `ToolExecutor` in the process. `agent.cancel()` already covers its own executor; use this for signal handlers or shutdown hooks that don't hold an agent reference.
