# SDK

Source: `mycode/src/mycode/`

## Package

`mycode-sdk` is a standalone Python SDK for multi-turn tool-calling agents. Import name: `mycode`. Ships independently of the CLI.

## Agent

```python
from mycode import Agent, tool


@tool
def word_count(text: str) -> int:
    """Count whitespace-separated words.

    Args:
        text: Text to count.
    """

    return len(text.split())


agent = Agent(
    model="claude-sonnet-4-6",
    api_key="YOUR_API_KEY",
    system="You are helpful.",
    tools=[word_count],   # default: no tools registered
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

Path attachments expand `~`; relative paths use the Python process's current working directory. An embedding application that owns a workspace should resolve its paths before passing them to the SDK.

### `run()` synchronous wrapper

`run()` consumes `achat()` via `asyncio.run`, concatenates `text` deltas into `RunResult.text`, captures the first error message in `RunResult.error`, and keeps the last `usage` payload in `RunResult.usage`. `RunResult.events` keeps the non-transient events, including final `tool_done` results; live `tool_output` deltas are omitted so synchronous runs do not retain complete command streams in memory.

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

`agent.clear()` drops the in-memory history without touching the on-disk log.

### Reasoning effort

`Agent(reasoning_effort=...)` passes the value directly to the selected provider adapter. Pass `None` to leave the effort unspecified. The adapter converts string values to the provider's request format and may reject unsupported values.

`ModelMetadata.reasoning_efforts` lists the effort values advertised for a model. It is informational and does not validate or restrict `Agent.reasoning_effort`.

### Cancellation

`agent.cancel()` can be called from another task or thread. It sets the cancel flag and cancels the active provider stream or tool task.

Tool cancellation is task cancellation. An async tool receives `asyncio.CancelledError` and either propagates it — the runtime then reports `error: cancelled` — or cleans up its own external resources (subprocesses, connections) and returns a final result, which becomes the error `tool_done`. Synchronous tools run in a worker thread that cancellation cannot interrupt: the turn ends immediately with `error: cancelled`, while the thread keeps running in the background until the tool returns; its late result is discarded, but external side effects still happen. Keep sync tools quick; implement long-running or cancellable work as `async def`.

A cancelled provider stream emits an `error` event with `message="cancelled"`. Already streamed `thinking` and text are persisted with `meta.stop_reason="cancelled"` when session persistence is enabled.

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

The Agent owns retries; provider SDK retries are disabled. Before reasoning or text reaches the caller, it retries connection errors, timeouts, stream-start expiry, HTTP 408/409/429/5xx, and transient stream failures identified by the provider adapter. Once reasoning or text has been emitted, failures surface as an `error` event; already-streamed reasoning/text is persisted with `meta.stop_reason="error"` and excluded from replay. Partial, unexposed tool-call arguments do not block a retry. User cancellation is never retried, and `cancel()` interrupts a backoff wait immediately.

Backoff is exponential with jitter (0.5s initial, ×2, capped at 8s); a positive `Retry-After` header up to 60s takes precedence. Each retry emits a `retry` event carrying `attempt` (the next 1-based attempt), `max_attempts` (`max_retries + 1`), `delay_seconds`, `reason` (`connection_error` / `request_timeout` / `stream_start_timeout` / `http_status` / `provider_error`), `message`, and `status_code` when applicable. Failed attempts without usage do not change turn totals. A retried request contributes the final successful response's usage; retried compaction stores that usage on its marker.

Adapters raise `ProviderError` (`reason`, `retryable`, `status_code`, `retry_after`); a stream-start expiry that exhausts its retries raises `StreamStartTimeoutError`, a subclass distinct from cancellation. Both are exported from `mycode`.

### Streaming events

`achat()` yields `Event(type, data)`:

| `type`           | `data`                                                                   |
| ---------------- | ------------------------------------------------------------------------ |
| `reasoning`      | `{"delta": str}`                                                         |
| `reasoning_done` | `{"duration_ms": int}`                                                   |
| `text`           | `{"delta": str}`                                                         |
| `tool_start`     | `{"tool_call": {"id", "name", "input"}}`                                 |
| `tool_output`    | `{"tool_use_id", "output"}`; delta from `streams_output=True` tools      |
| `tool_done`      | `{"tool_use_id", "output", "is_error", "metadata"?, "content"?}`         |
| `compact`        | `{}`; emitted right after a compact marker is appended                   |
| `retry`          | fields under Timeouts and retries; emitted before each new attempt       |
| `usage`          | see below; emitted after every provider request                          |
| `error`          | `{"message"}`; fatal for the turn, then the iterator stops               |

### Usage and cost

A `usage` event follows each successful provider request, including automatic compaction. It reports the latest context occupancy and best-effort cumulative usage and cost for the turn:

```python
{
    "context_tokens": 1456,        # latest normal request's usage.total_tokens
    "turn_usage": {                # cumulative for the turn
        "total_tokens": 2912, "input_tokens": 2800,
        "output_tokens": 112,
    },
    "turn_cost": {
        "input": 0.0042,
        "output": 0.0081,
        "total": 0.0123,
    },
}
```

- `context_tokens` is the latest normal request's context usage. It is not cumulative.
- `turn_usage` sums each reported token field. Missing fields do not clear known totals.
- `turn_cost.total` sums requests with known costs. Requests without cost are skipped; `turn_cost` is `None` only when no cost is known.
- Detailed components are summed while every known request has them. If any known request reports only `total`, the cumulative cost contains only `total`.
- Failed and cancelled requests without final usage do not emit an extra `usage` event.

Each completed request persists token facts in `meta.usage` and its fixed USD cost in `meta.cost`; see docs/sessions.md. `estimate_cost(usage, pricing)` uses `ModelMetadata.pricing` from models.dev and applies long-context tiers per request. Missing cache/reasoning prices use base input/output prices. Missing required totals or prices and inconsistent token counts return `None`. OpenRouter's reported charge is persisted directly as `{"total": ...}`. Historical costs are never recomputed.

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

Runtime-only fields are **not** persisted: the `system` prompt, `api_key`, `api_base`, registered `tools`, and `deps`. Per-turn `provider` and `model` travel as `meta` on the individual assistant message.

The session subdirectory is created lazily. Constructing an `Agent` with an unused `session_id` does not write anything. `<session_dir>/<session_id>/` and `messages.jsonl` appear when the first message is persisted. The `session_dir` root is created on `Agent` construction.

### Resolving `session_dir` and `session_id`

| `session_dir` | `session_id`                                   | behaviour                                                        |
| ------------- | ---------------------------------------------- | ---------------------------------------------------------------- |
| `None`        | any                                            | no persistence; a runtime-only uuid is assigned when omitted     |
| `Path(...)`   | `None`                                         | persistence on; a fresh uuid is allocated                        |
| `Path(...)`   | `"X"`, `<dir>/X/messages.jsonl` does not exist | new timeline; files are created on the first persisted message   |
| `Path(...)`   | `"X"`, `<dir>/X/messages.jsonl` exists         | history auto-loaded into `agent.messages` during `__init__`      |

Construct an `Agent` with the same `(session_dir, session_id)` to resume across processes. Passing `messages=[]` or `messages=[...]` for an existing session raises `ValueError` because it would conflict with the JSONL log.

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

See `docs/sessions.md` for the on-disk record format, the projection rule that builds the provider-facing view, and the replay rules applied by `SessionStore.load_messages`.

## Tools

Tools are opted in via `tools=[...]`; nothing is registered by default. A `streams_output=True` tool streams display text through `tool_output` events; other tools return a single `tool_done` result.

`agent.tools` is the session's `ToolExecutor`. Its read-only `specs` tuple exposes the registered `ToolSpec` values in definition order, which is useful when constructing another agent with the same tool set. `definitions` remains the provider-facing JSON representation.

`tool_output` is ordered, append-only display text. Consumers do not insert separators. When a slow consumer exceeds the per-call pending limit, the stream drops one continuous middle segment and inserts `[live output omitted]` on its own line. `tool_done.output` remains the authoritative result.

A tool call is rejected before hooks and execution when the assistant turn has `stop_reason="length"`, the tool block has `meta.invalid_input=true`, or the provider finish reason is `unknown`. The runtime still emits `tool_start` and an error `tool_done`, persists that as a `tool_result`, and sends it in the next provider request. An `invalid_input` rejection includes the tool name, `meta.parse_error`, and the original text from `meta.raw_arguments`. A canonical `error` response ends the turn with an `error` event. `@tool` schema validation prevents invalid arguments from reaching the user function.

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

    import httpx2

    async with httpx2.AsyncClient() as client:
        response = await client.get(url)
        return response.text
```

The Agent awaits async tools on its event loop and runs sync tools in a worker thread, so a sync tool never blocks the stream. Sync tools cannot be interrupted once running — use them for quick operations and implement long-running or cancellable work as `async def` (see Cancellation). Use `ToolExecutor.aexecute()` from async code and `ToolExecutor.execute()` from sync code.

A bare `str` return becomes the tool output replayed to the provider. Other JSON-serializable returns are dumped to JSON. Return `ToolExecutionResult` when you need `content`, `metadata`, or `is_error`.

### Application dependencies and `ToolContext`

`Agent(deps=...)` accepts any application-owned context object. The SDK keeps it opaque, does not persist it, and passes the same object to every tool and tool hook. Put stable per-agent dependencies here, such as a workspace path, database client, or output directory.

Annotate the first parameter of a tool as `ToolContext[Deps]` to have the runtime context injected with typed access to `ctx.deps`:

```python
from dataclasses import dataclass
from pathlib import Path

from mycode import Agent, ToolContext, tool


@dataclass(frozen=True)
class AppDeps:
    workspace: Path


@tool
def read_note(ctx: ToolContext[AppDeps], path: str) -> str:
    """Read a note from the workspace."""

    return (ctx.deps.workspace / path).read_text()


agent = Agent(model="...", api_key="...", deps=AppDeps(Path("/workspace")), tools=[read_note])
```

- `ctx.tool_call_id` carries the provider tool-call id on agent-loop calls.
- `ctx.call(name, args)` and `await ctx.acall(name, args)` dispatch any registered tool by name. A nested call shares the outer call's context, including `emit` and `tool_call_id`.
- `ctx.emit(delta)` appends display text for a `streams_output=True` tool. It is thread-safe and works from sync and async tools. Event boundaries do not imply line boundaries; the runtime may replace a continuous middle segment with `[live output omitted]` under buffer pressure.

### Tool hooks

`Hooks` lets SDK callers observe or replace model-requested tool executions without changing the provider protocol or message format:

```python
import os

from mycode import Agent, Hooks, ToolExecutionResult, tool

hooks = Hooks()


@tool
def delete_file(path: str) -> str:
    """Delete a file.

    Args:
        path: Path of the file to delete.
    """

    os.remove(path)
    return f"deleted {path}"


@hooks.before_tool
async def protect_dotfiles(ctx):
    if ctx.tool_name == "delete_file" and str(ctx.tool_input.get("path") or "").startswith("."):
        return ToolExecutionResult(output="error: blocked by hook", is_error=True)
    return None


@hooks.after_tool
async def audit(_ctx, _result):
    return None


agent = Agent(model="...", api_key="...", tools=[delete_file], hooks=hooks)
```

`ToolHookContext[Deps]` carries the same `deps` object as `ToolContext[Deps]`, plus `session_id`, `provider`, `model`, `tool_call_id`, `tool_name`, `tool_input`, and `tool` (the `ToolSpec`). `tool_input` is recursively frozen: nested dicts become `MappingProxyType` and lists become tuples. Hooks cannot mutate what the UI shows or the tool receives.

- `before_tool(ctx)` hooks run in registration order. Returning `None` continues; returning a `ToolExecutionResult` skips the real tool and uses that result.
- `after_tool(ctx, result)` hooks run in registration order for both real and skipped results. Returning `None` keeps the current result; returning a `ToolExecutionResult` replaces it for later hooks and the final `tool_done` event.
- `tool_start` is emitted before `before_tool` runs, so a hook that blocks (e.g. waiting on an external review) keeps the call visible in the event stream while it waits.
- `before_tool` exceptions become `ToolExecutionResult(output="error: tool hook failed: ...", is_error=True)`, the real tool is not run, and later `before_tool` hooks are skipped.
- `after_tool` exceptions are logged and the existing tool result is forwarded unchanged. Later `after_tool` hooks are skipped. Hooks that need to fail closed (e.g. redaction) must catch internally and return an explicit error result.
- Cancellation is controlled by the runtime. Cancelled tool results do not run `after_tool` hooks and cannot be replaced.
- Streaming tools still stream `tool_output` during real execution. If a `before_tool` hook skips the tool, no live `tool_output` events are emitted.
