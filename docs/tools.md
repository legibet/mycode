# Built-in Tools

Source: `cli/src/mycode_cli/tools.py`

The CLI registers `DEFAULT_TOOLS` — `read`, `write`, `edit`, `bash` — on every agent it builds. They are ordinary `ToolSpec` values on the SDK tool runtime; streaming, cancellation, and hook behavior follow the contract in `docs/sdk.md`. The output text formats below are a cross-component contract: the TUI and web UI render them directly.

## read

Reads a UTF-8 text file or a supported image.

- `offset` is a 1-indexed starting line; `limit` caps returned lines (default 2000). An offset beyond the end of the file is an error.
- When more lines remain, the output ends with `[Showing lines A-B. Use offset=N to continue.]`.
- Lines longer than 2000 chars are shortened with ` ... [line truncated]`; a trailing notice cites the first shortened line and gives byte-range bash commands for inspecting it.
- Image files return a text summary plus an image block. If the model does not accept image input, the call is an error.

## write

Creates or completely overwrites a file, creating parent directories as needed. The write is atomic: content goes to a temp file which replaces the target.

## edit

Applies a list of `{oldText, newText}` replacements. All entries match against the original file content and are applied together.

- Each `oldText` must identify exactly one region. Exact match is tried first; a fuzzy fallback tolerates line-ending and trailing-whitespace differences while still replacing only the matched original region. Zero matches (the error cites the closest line) or multiple matches fail the call.
- Overlapping edits are an error. If the file's mtime changed between read and write, the call fails with `file changed while editing`.
- The result output is `Updated <path>`; `metadata` carries `patch` (a standard unified diff) plus `added_lines` / `removed_lines` counted from that patch, so TUI and web show identical stats.

## bash

Runs a shell command in the agent's `cwd`. stdout and stderr are combined; stdin is `/dev/null`. On POSIX the command runs in its own process group, and every kill below targets the group.

- **Streaming**: output is emitted as display deltas while the command runs. Delta boundaries do not imply line boundaries.
- **Truncation**: the result is a bounded tail — at most 2000 lines and 50KB, whichever cuts first. When output exceeds either limit, the full raw bytes are written to `<tool_output_dir>/bash-<tool_call_id>.log` (see `docs/sessions.md`) and the result appends an `[Output truncated: ...]` notice citing that path.
- **Timeout**: default 120s, overridden by the `timeout` argument. On expiry the process group is killed and the result is the captured tail plus `[Command timed out after <N>s]` with `is_error=true`.
- **Cancellation**: bash handles task cancellation itself — it kills the process group, drains remaining output, and returns the captured tail plus `error: cancelled` with `is_error=true`.
- **Exit code**: non-zero exit appends `[exit code: N]` and sets `is_error=true`. Empty output renders as `(empty)`.
