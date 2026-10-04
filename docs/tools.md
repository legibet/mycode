# Built-in Tools

Sources: `cli/src/mycode_cli/tools.py`, `cli/src/mycode_cli/web_tools.py`, `cli/src/mycode_cli/permissions.py`

The CLI always registers `read`, `write`, `edit`, `bash`, and `webfetch`. It registers `websearch` when `web.search` selects a provider. They are ordinary `ToolSpec` values on the SDK tool runtime; streaming, cancellation, and hook behavior follow the contract in `docs/sdk.md`. The output text formats below are a cross-component contract: the TUI and web UI render them directly.

## read

Reads a UTF-8 text file or a supported image.

- `offset` is a 1-indexed starting line; `limit` caps returned lines (default 2000). An offset beyond the end of the file is an error.
- Output is also capped at 50KB of UTF-8, whatever `limit` is.
- When more lines remain, the output ends with `[Showing lines A-B of <path>. Use read with offset=N to continue.]`.
- Lines longer than 2000 chars are shortened with `... [line truncated]`; a trailing notice cites the first shortened line and gives byte-range bash commands for inspecting it.
- `@path` text attachments use the same window and notices.
- Image files return a text summary plus an image block. If the model does not accept image input, the call is an error.

## write

Creates or completely overwrites a file, creating parent directories as needed. The write is atomic: content goes to a temp file which replaces the target.

## edit

Applies a list of `{oldText, newText}` replacements. All entries match against the original file content and are applied together.

- Each `oldText` must identify exactly one region. Exact match is tried first; a fuzzy fallback tolerates line-ending and trailing-whitespace differences while still replacing only the matched original region. Zero matches (the error cites the closest line) or multiple matches fail the call.
- Overlapping edits are an error. If the file's mtime changed between read and write, the call fails with `file changed while editing`.
- The result output is `Updated <path>`; `metadata` carries `patch` (a standard unified diff) plus `added_lines` / `removed_lines` counted from that patch, so TUI and web show identical stats.

## bash

Runs `bash -c <command>` in the CLI workspace (`CliDeps.cwd`), using the first `bash` on `PATH`. bash starts non-interactive and non-login, so the command sees the CLI process environment as-is; no profile or rc file is sourced. stdout and stderr are combined; stdin is `/dev/null`. On POSIX the command runs in its own process group, and every kill below targets the group.

- **Streaming**: output is emitted as display deltas while the command runs. Delta boundaries do not imply line boundaries.
- **Truncation**: the result is a bounded tail — at most 2000 lines and 50KB, whichever cuts first. When output exceeds either limit, the full raw bytes are written to `<tool_output_dir>/bash-<tool_call_id>.log` (see `docs/sessions.md`) and the result appends an `[Output truncated: ...]` notice citing that path.
- **Timeout**: default 120s, overridden by the `timeout` argument. On expiry the process group is killed and the result is the captured tail plus `[Command timed out after <N>s]` with `is_error=true`.
- **Cancellation**: bash handles task cancellation itself — it kills the process group, drains remaining output, and returns the captured tail plus `error: cancelled` with `is_error=true`.
- **Exit code**: non-zero exit appends `[exit code: N]` and sets `is_error=true`. Empty output renders as `(empty)`.
- **Missing bash**: if no `bash` is on `PATH`, the result is `error: bash not found on PATH` with `is_error=true`.

### Background commands

`background=true` starts the command and returns at once. Use it for long commands the model does not need to wait for; `timeout` is ignored.

- **Start result**: `is_error=false`, metadata `{"background": true, "pid": <pid>, "log": "<path>"}`, and this text:

  ```text
  Started in background (pid 12345): pytest -q
  Log: <tool_output_dir>/bash-<tool_call_id>.log
  ```

  The tool description tells the model not to poll and to end its turn when nothing else is left; the result text itself carries only facts, since both UIs show it.

- **Log file**: opened before the call returns and flushed after every chunk, so `read` on it shows live output. Reads inside `tool_output_dir` need no approval.
- **Notification**: when the command exits, the host delivers a text block carrying `meta.job` (`docs/sessions.md`) to the model: a header `Background bash finished (pid <pid>, exit code <N>): <command>` and `Log: <path>`, then the bounded tail in the foreground format. A process ended by a signal reports the negative return code. The header carries the exit code, so no `[exit code: N]` trailer follows. When the output cannot be captured (such as a log file that cannot be written), the notification still arrives, with `error: <reason>` after whatever output was captured.
- **Delivery**: the result is steered into the running turn, becomes the next turn when the running turn is finishing, or wakes the idle session as a new turn. Stop (TUI Esc, web Stop) or a permission `Deny` suspends wakes until the user's next message, which then carries the result ahead of its text. The result waits in the session's job registry until a run has committed it, so an interrupted or failed run never loses it, and it is never delivered twice.
- **Lifecycle**: `/rewind`, `rewind_to`, `/new`, a completed `/resume`, session delete, and server shutdown kill the session's background commands and drop undelivered results. The web UI's Stop (`DELETE /api/sessions/{id}/jobs/{tool_use_id}`) and a `kill <pid>` through a normal `bash` call end one command early; its result is still delivered, with the signal's negative exit code. No command survives the process.
- **Permission**: classified from the command text as usual; the review preview ends with ` (background)`.
- **Hosts without delivery**: `mycode run` ends with the turn, so it returns `error: background commands are not available in this mode` with `is_error=true` and spawns nothing.

## webfetch

Reads one HTTP or HTTPS URL using the implementation selected by `web.fetch`: local HTTP, Tavily Extract, or Exa Contents. HTML is returned as Markdown; Markdown, text, JSON, and XML are returned as text. Images, PDFs, and other binary MIME types return `error: unsupported content type: <mime>`.

- `timeout` is a whole-call budget, including redirects, the local 403 User-Agent retry, provider requests, and response reading. It defaults to 30 seconds and is clamped to 1–120. Timeout errors tell the model it may retry with a larger value.
- Every HTTP response is streamed with a 5MB decoded-body cap. Responses over the limit return `error: response too large (over 5MB)`.
- Output keeps the first 2000 lines or 50KB. When truncated, the complete converted content is written to `<tool_output_dir>/webfetch-<tool_call_id>.md`; the result ends with `[Output truncated: ...]` naming the path and telling the model to use `read`.
- A redirect appends `[Redirected to <final_url>]`. Provider failures and HTTP failures use lowercase `error:` results with `is_error=true`. Implementations never fall back to another provider.

The local converter removes non-rendering HTML tags and data-URI images, then converts the full body to Markdown. It does not select an article or remove navigation, headers, or footers.

## websearch

Searches with the configured Tavily or Exa provider and returns matching pages with short excerpts. Each numbered result contains a title, URL, and up to 400 excerpt characters. It never requests full page text or summaries; use `webfetch` to read a page. Zero results returns `No results found.` and is not an error.

- `max_results` defaults to 5 and is clamped to 1–10.
- `recency` accepts `day`, `week`, `month`, or `year`; domain include/exclude filters are passed to the provider.
- `search_depth` defaults to `balanced`; `fast` lowers latency and `deep` is reserved for searches where the normal mode is insufficient.
- The internal whole-call timeout is fixed at 30 seconds. The tool has no timeout parameter.
- Result metadata is `{"results": N}`, used by the WebUI collapsed suffix.

## Permissions

`permissions.py` classifies every tool call in a `before_tool` hook before execution. The `permission.level`/`mode` fields are defined in `docs/config.md`; this section is the classification contract.

Both interactive surfaces (TUI and web) prompt for approval when `mode: "ask"` and the call falls outside the configured level. Non-interactive `mycode run` has no prompt and treats `ask` as `deny`. Automatic denials do not stop the run; the model receives the denied tool result and can reply with next steps. An explicit user `Deny` cancels the current run in both TUI and web.

The shell checks are intentionally simple and conservative. Project commands such as tests, builds, formatters, package scripts, and task runners are `standard` because they execute project-defined code. Compound commands (`&&`, `||`, `;`, pipes, redirection, command substitution) and obvious destructive commands (`rm`, `sudo`, `chmod`, `git reset`, `git clean`, `git push --force`, etc.) fall outside `readonly`/`safe`/`standard` and require `yolo` or `mode: "ask"` approval.

`webfetch` and `websearch` are `standard`. Their permission previews show the initial URL and query respectively. Reads inside the current session's `tool_output_dir` are `readonly`, including follow-up reads of truncated webfetch output.

Local webfetch intentionally does not block localhost, private address ranges, or cloud metadata endpoints. The machine is the trust boundary. The permission prompt displays the initial URL only; redirects are followed up to five times without another prompt, so a public URL can redirect to a private or metadata address.
