# CLI

Source: `cli/src/mycode_cli/main.py`. Terminal UI keys and slash commands: `cli/README.md`.

## Commands

```text
mycode [OPTIONS]                 interactive session
mycode run [OPTIONS] [PROMPT]    send one prompt, print the outcome, exit
mycode web [OPTIONS]             web UI server
mycode session list [--all]      list saved sessions for the current workspace, or all of them
```

`-h` / `--help` works on every command; `mycode -V` / `--version` prints the version. `mycode session` without a subcommand prints its help.

### Session options

`mycode` and `mycode run` take the same options:

| option                    | meaning                                                                                |
| ------------------------- | -------------------------------------------------------------------------------------- |
| `--provider NAME`         | provider id or configured alias                                                        |
| `--model NAME`            | model name                                                                             |
| `--effort LEVEL`          | reasoning effort                                                                       |
| `--permission LEVEL`      | tool permission level: `readonly`, `safe`, `standard`, `yolo`                          |
| `--max-turns N`           | limit provider requests per user turn                                                  |
| `--session ID`            | resume a session; an unknown id is an error                                            |
| `-c`, `--continue`        | resume the most recent session in the current workspace, or start a new one if none    |

`--session` and `--continue` are mutually exclusive. Without either, a new session starts. `--provider`, `--model`, and `--effort` start from the remembered selection and are not saved (`docs/config.md`, Remembered Selection); `--permission` overrides the configured level and keeps the configured `permission.mode`.

Options belong to the command they follow. Session options placed before `run`, `web`, or `session` are a usage error instead of being ignored: `mycode run --model x "..."`, not `mycode --model x run "..."`.

### `mycode run`

The prompt is the joined `PROMPT` arguments. When stdin is not a terminal it is read to the end: appended to the prompt after a blank line, or used as the prompt when no arguments are given. An empty prompt is a usage error.

```bash
mycode run "explain the session store"
git diff | mycode run "review this change"
mycode run < task.md
```

The run has no approval prompt, so `permission.mode: "ask"` acts as `deny` (`docs/tools.md`). Background `bash` commands are unavailable.

`--format` selects the output: `text` (default), `json`, or `jsonl`; see Output formats.

### `mycode web`

| option        | meaning                                                         |
| ------------- | --------------------------------------------------------------- |
| `--host HOST` | address to listen on; default `127.0.0.1`                       |
| `--port PORT` | port; default `$PORT`, else `8000`                              |
| `--dev`       | serve the API only, with hot reload, for Vite development       |

Serving modes and CORS: `docs/api.md`.

## Output formats

stdout carries only the selected output; diagnostics go to stderr.

- **`text`** prints the final assistant reply. An error or permission denial without a reply goes to stderr.
- **`json`** prints one `result` record when the run ends.
- **`jsonl`** prints one JSON object per line: an `init` record, every agent event as it happens, then the `result` record.

### `init`

```json
{"type": "init", "session_id": "3f2a…", "cwd": "/repo", "provider": "anthropic", "model": "claude-sonnet-5", "effort": "high", "permission": "safe", "version": "0.9.0"}
```

`provider` is the provider name as configured (an alias for custom providers). `effort` is `null` when unset (`auto`).

### Events

Between `init` and `result`, each line is an SDK event's `to_dict()` payload: `text`, `reasoning`, `reasoning_done`, `tool_start`, `tool_output`, `tool_done`, `usage`, `retry`, `compact`, `error`. Names and fields are those in `docs/sdk.md`, Streaming events; the same events feed the SSE stream, without its `seq` and server-added fields. As over SSE, `tool_done` omits the multimodal `content`. `text` and `reasoning` are deltas; `result.result` holds the joined reply.

### `result`

```json
{
  "type": "result",
  "is_error": false,
  "result": "The session store appends records to …",
  "error": null,
  "session_id": "3f2a…",
  "provider": "anthropic",
  "model": "claude-sonnet-5",
  "duration_ms": 18234,
  "usage": {"total_tokens": 21480, "input_tokens": 20915, "output_tokens": 565},
  "cost": {"total": 0.0712, "input": 0.0627, "output": 0.0085},
  "permission_denials": []
}
```

- `result` is the text of the last committed assistant message, `""` if there is none.
- `is_error` is `true` exactly when the exit code is `1`. `error` is the agent's error message (for example `max_turns reached`), the message of an exception that ended the run (such as a failed session write), or, for a permission denial, the denied tool's output; `null` otherwise. An error does not clear `result`: the model may still have replied.
- `usage` and `cost` are the turn totals from the last `usage` event, in the `Usage` / `Cost` shapes of `docs/sdk.md`; `cost` is in USD and `null` when unknown.
- `duration_ms` is wall-clock time from the start of the run.
- `permission_denials` lists each denied tool call as `{"tool_use_id", "name", "input"}`.

A failure before the agent starts (unknown session, no usable provider, invalid effort) prints a `result` with `is_error: true`, the message in `error`, `null` session/provider/model, and no `init` line.

## Exit codes

| code | meaning                                                                                      |
| ---- | -------------------------------------------------------------------------------------------- |
| `0`  | success                                                                                      |
| `1`  | run or startup error, or a denied tool call; in `json` / `jsonl` the `result` record says why |
| `2`  | usage error (unknown option, invalid value, missing prompt); message on stderr               |

The codes are the same for every output format.
