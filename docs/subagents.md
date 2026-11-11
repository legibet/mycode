# Subagents

Sources: `cli/src/mycode_cli/agent_tool.py`, `cli/src/mycode_cli/agents.py`, `cli/src/mycode_cli/runtime.py`

The `agent` tool hands a task to a subagent: a child agent that works in its own session, stored under the parent's (`docs/sessions.md`, Subagent sessions). The parent's history holds only the call, its result, and the notification of a background agent. The TUI, the web server, and `mycode run` register the tool. A subagent does not, so subagents never nest.

## Parameters

| parameter     | type   | meaning                                                                    |
| ------------- | ------ | -------------------------------------------------------------------------- |
| `description` | string | Three to five words naming the task, shown in both UIs                     |
| `prompt`      | string | The task; the child starts with an empty conversation                      |
| `agent`       | string | Definition name, default `general`                                         |
| `model`       | string | One of the usable `subagent.models`; the parameter is absent when none is |
| `effort`      | string | `low`, `medium`, `high` or `xhigh`; defaults to the parent's effort       |
| `background`  | bool   | Return at once and deliver the result when the child finishes             |
| `resume`      | string | Agent id from an earlier call: continue that child                         |

A resume ignores `agent`, `model` and `effort`. The tool description lists the definitions as `- name: description`.

## Definitions

`general` is built in: the parent's tools, with nothing added to the prompt. Definition files are markdown with YAML frontmatter, read from `$MYCODE_HOME/agents/*.md` and then from `.mycode/agents/*.md` in each directory from the project root down to the current directory. A later file replaces an earlier one of the same name, including the built-in `general`. Files are read when the parent agent is built. An invalid file is logged and skipped.

| field         | required | meaning                                                            |
| ------------- | -------- | ------------------------------------------------------------------ |
| `name`        | no       | Letters, digits, `-` and `_`; defaults to the file name           |
| `description` | yes      | One line, listed in the tool description                           |
| `model`       | no       | A model reference                                                  |
| `permission`  | no       | Same values as the config `permission`; narrows the parent's       |
| `max_turns`   | no       | Positive integer: model calls per turn; a turn that needs more fails |

The body is appended to the child's system prompt.

## Child

The child's system prompt is the parent's (base prompt, `AGENTS.md`, skills), then a paragraph saying that its final message goes to the calling agent instead of the user, then the definition body. Its first message is `prompt`.

The child runs on the first of these that is set:

1. the call's `model`;
2. the definition's `model`;
3. the first usable entry of `subagent.models` (`docs/config.md`);
4. the parent's provider and model.

References resolve like `--model` (`docs/cli.md`, Model selection), preferring the parent's provider. When the parent agent is built, a `subagent.models` entry that does not resolve is dropped with a warning. A definition's `model` that does not resolve fails the call.

The child's reasoning effort is the call's `effort`, else the parent's. When the child's model does not accept that value, the child uses the next higher effort the model accepts, or its highest. A model that accepts no effort, or a parent without one, leaves it to the provider's default.

The child's permission is the parent's, narrowed by the definition's: it takes the lower level, and `deny` when either side denies. A background child always runs in `deny` mode, because nobody can answer its requests. In `deny` mode the child only gets tools that some call could pass at its level: `read` and `bash` from `readonly`, `write` and `edit` from `safe`, `webfetch` and `websearch` from `standard` (`docs/tools.md`, Permissions). A blocking child's approval requests go to the user through the parent, labeled with the description. Denying one stops the parent's turn, and the child with it.

The child has no job registry, so its `bash` cannot run in the background. There is no limit on running children and no timeout.

## Calls and results

A new child gets a generated id (a uuid hex). As soon as the child exists, the call emits a `tool_output` with empty text and metadata `{"agent", "session_id", "background"}`, so a UI can link the running call to its child. Every result carries the same fields.

While a blocking call runs, it streams one line per child step: `Read <path>`, `Write <path>`, `Edit <path>`, `Bash <command>`, `WebFetch <url>`, `WebSearch <query>`, the name of any other tool, and the first line of each text reply. These lines are not persisted. A completed call returns the child's final reply and a notice:

```text
<final reply>

[Agent <id> finished. Continue it with resume="<id>".]
```

A failed, cancelled, or `max_turns`-limited turn returns the text the child produced, then `error: <reason>`, with `is_error=true`. A cancelled turn's reason tells the model how to continue:

```text
error: cancelled by the user after 7 tool calls; the agent's transcript is kept, continue it with resume="<id>"
```

Cancelling the parent's turn cancels its blocking children. The result metadata is for the UIs:

```json
{"agent": "general", "session_id": "<child id>", "background": false, "status": "completed", "tool_calls": 12, "duration_ms": 45210, "usage": {...}, "cost": {"total": 0.03}}
```

`status` is `completed`, `failed`, or `cancelled`. `usage` and `cost` sum the records the turn added to the child session.

## Background agents

With `background=true`, the call registers a background job (`docs/tools.md`, Background commands) and returns `Started agent in background: <description>` and `Agent id: <id>`. When the child's turn ends, the parent receives a notification that reads `Agent finished: <description>`, followed by the reply in the blocking format without the notice. Delivery, Stop, and lifecycle follow background commands. A stopped agent still delivers its result, with `status: "cancelled"`. `mycode run` has no job registry and returns `error: background agents are not available in this mode`.

## Resume

A resume of a running background child sends `prompt` to its current turn as a steer and returns `Message delivered to running agent <id>`. A child that is finishing its turn refuses with `error: agent <id> is finishing; wait for its result, then resume it`.

Any other resume starts a new turn in the child session, blocking or in the background, on the provider and model stored when the child was created and with the current definition body. Turns of one child run one at a time.

## Errors

| result                                                    | cause                                       |
| --------------------------------------------------------- | ------------------------------------------- |
| `error: invalid agent id: '<id>'`                         | `resume` does not match `^[A-Za-z0-9_-]{1,128}$` |
| `error: unknown agent id: <id>`                           | `resume` names no child                     |
| `error: unknown agent: <name>`                            | `agent`, or a resumed child's definition, no longer exists; a new call also lists the available ones |
| `error: unknown model: <model>; available: ...`           | `model` is not a usable `subagent.models` entry |
| `error: model '<ref>' of agent '<name>' is not available` | the definition's `model` does not resolve   |
| `error: session <id> is in use by ...`                    | another process holds the child             |
