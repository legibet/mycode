# Built-in Tools

Sources: `cli/src/mycode_cli/tools.py`, `cli/src/mycode_cli/web_tools.py`, `cli/src/mycode_cli/attachments.py`, `cli/src/mycode_cli/permissions.py`

The CLI always registers `read`, `write`, `edit`, `bash`, and `webfetch`. It registers `websearch` when `web.search` selects a provider, and `agent` everywhere except inside a subagent (`docs/subagents.md`). They are ordinary `ToolSpec` values on the SDK tool runtime; streaming, cancellation, and hook behavior follow the contract in `docs/sdk.md`. `read`, `webfetch`, `websearch`, and `agent` are parallel-safe, so consecutive calls to them run concurrently; `write`, `edit`, and `bash` run one at a time. The output text formats below are a cross-component contract: the TUI and web UI render them directly.

## read

Reads a UTF-8 text file or an image.

- `offset` is a 1-indexed starting line; `limit` caps returned lines (default 2000). An offset beyond the end of the file is an error.
- Output is also capped at 50KB of UTF-8, whatever `limit` is.
- When more lines remain, the output ends with `[Showing lines A-B of <path>. Use read with offset=N to continue.]`.
- Lines longer than 2000 chars are shortened with `... [line truncated]`; a trailing notice cites the first shortened line and gives byte-range bash commands for inspecting it.
- A PNG, JPEG, GIF or WebP file returns a text summary plus an image block. An image of at most 2000px per side and 5MB of base64 is returned as it is; a larger one is scaled down to fit and re-encoded as JPEG.
- An image file over 20MB or one that cannot be decoded is an error, and so is any image when the model does not accept image input.

## write

Creates or completely overwrites a file, creating parent directories as needed. The write is atomic: content goes to a temp file which replaces the target.

## edit

Applies a list of `{oldText, newText}` replacements. All entries match against the original file content and are applied together.

- Each `oldText` must identify exactly one region. Exact match is tried first; a fuzzy fallback tolerates line-ending and trailing-whitespace differences while still replacing only the matched original region. Zero matches (the error cites the closest line) or multiple matches fail the call.
- Overlapping edits are an error. If the file's mtime changed between read and write, the call fails with `file changed while editing`.
- The result output is `Updated <path>`; `metadata` carries `patch` (a standard unified diff) plus `added_lines` / `removed_lines` counted from that patch, so TUI and web show identical stats.
- `metadata.highlight_context` has one `{old, new}` entry per hunk: each file's whole lines just before the hunk, at most 40 lines and 4000 characters. The web passes each side's text to the syntax highlighter as preceding code, so a hunk that starts inside a multi-line string or comment usually starts in the right state. It is best-effort: a construct opened before that window is still missed.

## bash

Runs `bash -c <command>` in the CLI workspace (`CliDeps.cwd`), using the first `bash` on `PATH`. bash starts non-interactive and non-login, so the command sees the CLI process environment as-is; no profile or rc file is sourced. stdout and stderr are combined; stdin is `/dev/null`. On POSIX the command runs in its own process group, and every kill below targets the group.

- **Streaming**: output is emitted as display deltas while the command runs. Delta boundaries do not imply line boundaries.
- **Truncation**: the result is a bounded tail — at most 2000 lines and 50KB, whichever cuts first. Lines end at `\n` only, so `\r` progress updates stay on their line, and the 50KB counts the text shown, without line terminators. When the raw output exceeds either limit, the full raw bytes are written to `<tool_output_dir>/bash-<id>.log`, named by an id generated for the call (see `docs/sessions.md`); when the tail drops output, the result appends an `[Output truncated: ...]` notice citing that path.
- **Timeout**: default 120s, overridden by the `timeout` argument. On expiry the process group is killed and the result is the captured tail plus `[Command timed out after <N>s]` with `is_error=true`.
- **Cancellation**: bash handles task cancellation itself — it kills the process group, drains remaining output, and returns the captured tail plus `error: cancelled` with `is_error=true`.
- **Exit code**: non-zero exit appends `[exit code: N]` and sets `is_error=true`. Empty output renders as `(empty)`.
- **Missing bash**: if no `bash` is on `PATH`, the result is `error: bash not found on PATH` with `is_error=true`.

### Background commands

`background=true` starts the command and returns at once. Use it for long commands the model does not need to wait for; `timeout` is ignored.

- **Start result**: `is_error=false`, metadata `{"background": true, "pid": <pid>, "log": "<path>", "job_id": "<id>"}`, and this text:

  ```text
  Started in background (pid 12345): pytest -q
  Log: <tool_output_dir>/bash-<job_id>.log
  ```

  `job_id` is the id of the background job (`docs/api.md`, `BackgroundJobInfo`), and it names the log.

  The tool description tells the model not to poll and to end its turn when nothing else is left; the result text itself carries only facts, since both UIs show it.

- **Log file**: opened before the call returns and flushed after every chunk, so `read` on it shows live output. Reads inside `tool_output_dir` need no approval.
- **Notification**: when the command exits, the host delivers a text block carrying `meta.job` (`docs/sessions.md`) to the model: a header `Background bash finished (pid <pid>, exit code <N>): <command>` and `Log: <path>`, then the bounded tail in the foreground format. A process ended by a signal reports the negative return code. A command the user stopped says `stopped by the user` in place of `finished`. The header carries the exit code, so no `[exit code: N]` trailer follows. When the output cannot be captured (such as a log file that cannot be written), the notification still arrives, with `error: <reason>` after whatever output was captured.
- **Delivery**: the result is steered into the running turn, becomes the next turn when the running turn is finishing, or wakes the idle session as a new turn. Stop (TUI Esc, web Stop) or a permission `Deny` suspends wakes until the user's next message, which then carries the result ahead of its text. The result waits in the session's job registry until a run has committed it, so an interrupted or failed run never loses it, and it is never delivered twice.
- **Lifecycle**: `/rewind`, `rewind_to`, `/new`, a completed `/resume`, session delete, and server shutdown kill the session's background commands and agents and drop undelivered results. The web UI's Stop (`DELETE /api/sessions/{id}/jobs/{job_id}`) and a `kill <pid>` through a normal `bash` call end one command early; its result is still delivered, with the signal's negative exit code, and after a Stop the header says the user stopped it. No command survives the process.
- **Permission**: classified from the command text as usual; the review preview ends with `(background)`.
- **Hosts without delivery**: `mycode run` and subagents have no job registry, so they return `error: background commands are not available in this mode` with `is_error=true` and spawn nothing.

## webfetch

Reads one HTTP or HTTPS URL using the implementation selected by `web.fetch`: local HTTP, Tavily Extract, or Exa Contents. HTML is returned as Markdown; Markdown, text, JSON, and XML are returned as text. Images, PDFs, and other binary MIME types return `error: unsupported content type: <mime>`.

- `timeout` is a whole-call budget, including redirects, the local 403 User-Agent retry, provider requests, and response reading. It defaults to 30 seconds and is clamped to 1–120. Timeout errors tell the model it may retry with a larger value.
- Every HTTP response is streamed with a 5MB decoded-body cap. Responses over the limit return `error: response too large (over 5MB)`.
- Output keeps the first 2000 lines or 50KB. When truncated, the complete converted content is written to `<tool_output_dir>/webfetch-<id>.md`, named by a generated id; the result ends with `[Output truncated: ...]` naming the path and telling the model to use `read`.
- A redirect appends `[Redirected to <final_url>]`. Provider failures and HTTP failures use lowercase `error:` results with `is_error=true`. Implementations never fall back to another provider.

The local converter removes non-rendering HTML tags and data-URI images, then converts the full body to Markdown. It does not select an article or remove navigation, headers, or footers.

## websearch

Searches with the configured Tavily or Exa provider and returns matching pages with short excerpts. Each numbered result contains a title, URL, and up to 400 excerpt characters. It never requests full page text or summaries; use `webfetch` to read a page. Zero results returns `No results found.` and is not an error.

- `max_results` defaults to 5 and is clamped to 1–10.
- `recency` accepts `day`, `week`, `month`, or `year`; domain include/exclude filters are passed to the provider.
- `search_depth` defaults to `balanced`; `fast` lowers latency and `deep` is reserved for searches where the normal mode is insufficient.
- The internal whole-call timeout is fixed at 30 seconds. The tool has no timeout parameter.
- Result metadata is `{"results": N}`, used by the WebUI collapsed suffix.

## Attachments

A file attached to a user message becomes one block, chosen by the file's content. This covers a TUI `@path`, a web upload, and a workspace file picked in the web `@` menu.

| File                             | Block                                                                                    |
| -------------------------------- | ---------------------------------------------------------------------------------------- |
| UTF-8 text                       | A `<file>` text block with the first `read` window and its notices.                      |
| Image                            | An `image` block, fitted as `read` fits an image.                                        |
| PDF of at most 20MB and 20 pages | A `document` block.                                                                      |
| Any other file                   | A `<file>` text block: `[Content not included: <reason>. The file is at <path>.]`        |

An image or PDF is also given by its path when the current model does not accept that input, when the file is over 20MB, and when it cannot be decoded. The model then inspects the file with its tools. PDFs are limited by pages because a PDF's token cost follows its page count, not its size.

A web upload has no path. One that the message does not hold whole is saved to `<tool_output_dir>/upload-<id>`, and the block cites that file (`docs/api.md`). The web client itself leaves out an image or PDF the current model does not accept, and any upload that is not UTF-8 text, an image or a PDF.

## Permissions

`permissions.py` classifies every tool call in a `before_tool` hook before execution. The `permission.level`/`mode` fields are defined in `docs/config.md`; this section is the classification contract.

Both interactive surfaces (TUI and web) prompt for approval when `mode: "ask"` and the call falls outside the configured level. Non-interactive `mycode run` has no prompt and treats `ask` as `deny`. Automatic denials do not stop the run; the model receives the denied tool result and can reply with next steps. Its output names the tool, its tier, and the configured level, after the fixed prefix `error: permission denied`:

```text
error: permission denied: bash call needs level yolo, this agent allows readonly
```

An explicit user `Deny` returns `error: permission denied by user` and cancels the current run in both TUI and web.

The shell checks are intentionally simple and conservative. `readonly` is a heuristic allowlist over the command text, not a sandbox: it means only that the command matches programs and flags known not to write or run anything else. It does not see what the environment adds, such as a repository whose git config makes `git status` run a program. A command is `readonly`, `standard`, or `yolo`:

- Allowlisted programs that only read are `readonly`: `ls`, `cat`, `rg`, `find`, `cd`, `echo` and similar, and the reading subcommands of `git` (`status`, `diff`, `log`, `show`, `grep`, `blame` and similar; `branch` and `remote` only with listing flags).
- Allowlisted programs lose `readonly` with arguments that write or execute: `env` with any argument, `sort` with `-o`/`--output`/`--compress-program`, `uniq` with a second file operand, `rg` with `--pre`/`--pre-glob`/`--hostname-bin`, `tree` with `-o`, `file` with `-C`, and `git` with `--output`, with `grep -O`/`--open-files-in-pager`, or with any option before the subcommand other than `-C <dir>`. `sed` is `readonly` only as `sed -n 'N,Mp' [file...]`.
- Project commands such as tests, builds, formatters, package scripts, and task runners are `standard` because they execute project-defined code; so is any command not on the allowlist, including `<program> --version`.
- A list or pipeline (split at `|`, `||`, `&&`, and `;` outside quotes and escapes) takes the highest level among its commands: `cd web && pnpm test` is `standard`, and `ls; rm -rf dist` is `yolo`.
- An unquoted `2>&1` word and a redirection to `/dev/null` write nothing, so they are ignored: `rg foo 2>/dev/null | head` is `readonly`.
- Commands that are not analyzed (`|&`, `&`, other redirections, subshells, command and process substitution, newlines, control flow such as `for` and `if`) and obvious destructive commands (`rm`, `sudo`, `chmod`, `git reset`, `git clean`, `git push --force`, etc.) are `yolo`: they require level `yolo` or `mode: "ask"` approval.

`agent` is `safe`, with the description as its preview; the child's own calls are checked when it makes them. `webfetch` and `websearch` are `standard`. Their permission previews show the initial URL and query respectively. Reads inside the current session's `tool_output_dir` or a discovered skill's directory are `readonly`, including follow-up reads of truncated webfetch output.

Local webfetch intentionally does not block localhost, private address ranges, or cloud metadata endpoints. The machine is the trust boundary. The permission prompt displays the initial URL only; redirects are followed up to five times without another prompt, so a public URL can redirect to a private or metadata address.
