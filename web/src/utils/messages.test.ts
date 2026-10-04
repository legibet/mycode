import { describe, expect, it } from "vitest";

import type { ChatMessage, RenderMessage } from "../types";
import { isChatMessage, isCompactMarker } from "../types";
import {
  buildRenderMessages,
  createUserMessage,
  markTailAssistantStopped,
  splitTurn,
  updateLatestThinkingDuration,
} from "./messages";

function expectChat(message: RenderMessage | undefined): ChatMessage {
  if (!message || !isChatMessage(message)) {
    throw new Error("expected a ChatMessage, got a marker or undefined");
  }
  return message;
}

describe("messages", () => {
  it("assigns sourceIndex to render user messages", () => {
    const renderMessages = buildRenderMessages([
      {
        role: "user",
        content: [{ type: "text", text: "first" }],
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "ack" }],
      },
      {
        role: "user",
        content: [{ type: "text", text: "second" }],
      },
    ]);

    const first = expectChat(renderMessages[0]);
    const third = expectChat(renderMessages[2]);

    expect(first.role).toBe("user");
    expect(first.sourceIndex).toBe(0);
    expect(third.role).toBe("user");
    expect(third.sourceIndex).toBe(2);
  });

  it("emits a compact marker entry between real turns", () => {
    const renderMessages = buildRenderMessages([
      {
        role: "user",
        content: [{ type: "text", text: "hello" }],
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "hi" }],
      },
      {
        role: "compact",
        content: [{ type: "text", text: "summary" }],
      },
      {
        role: "user",
        content: [{ type: "text", text: "follow-up" }],
      },
    ]);

    expect(renderMessages).toHaveLength(4);
    const marker = renderMessages[2];
    expect(marker && isCompactMarker(marker)).toBe(true);
    if (marker && isCompactMarker(marker)) {
      expect(marker.sourceIndex).toBe(2);
      expect(marker.renderKey).toBe("compact:2");
    }
    expect(expectChat(renderMessages[3]).role).toBe("user");
  });

  it("keeps document blocks in user render messages", () => {
    const renderMessages = buildRenderMessages([
      {
        role: "user",
        content: [
          { type: "text", text: "summarize this" },
          {
            type: "document",
            data: "JVBERi0xLjc=",
            mime_type: "application/pdf",
            name: "report.pdf",
          },
        ],
      },
    ]);

    expect(renderMessages).toEqual([
      {
        role: "user",
        renderKey: "user:0",
        sourceIndex: 0,
        content: [
          {
            type: "text",
            text: "summarize this",
            renderKey: "user:0:0",
          },
          {
            type: "document",
            data: "JVBERi0xLjc=",
            mime_type: "application/pdf",
            name: "report.pdf",
            renderKey: "user:0:1",
          },
        ],
      },
    ]);
  });

  it("keeps skill snapshots out of rendered user messages", () => {
    const renderMessages = buildRenderMessages([
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "<skill>private instructions</skill>",
            meta: { skill_snapshot: true },
          },
          { type: "text", text: "Use /ui here" },
        ],
      },
    ]);

    const content = expectChat(renderMessages[0]).content;
    expect(content).toHaveLength(1);
    expect(content[0]).toMatchObject({ type: "text", text: "Use /ui here" });
  });

  it("opens a wake turn with a job marker and marks the call that started the job", () => {
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "run tests later" }] },
      {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "call-1",
            name: "bash",
            input: { command: "pytest", background: true },
          },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "call-1",
            output: "Started in background (pid 42): pytest\nLog: /tmp/job.log",
            metadata: { background: true, pid: 42, log: "/tmp/job.log" },
            is_error: false,
          },
        ],
      },
      { role: "assistant", content: [{ type: "text", text: "started" }] },
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "Background bash finished (pid 42, exit code 1): pytest\nLog: /tmp/job.log\n\nFAILED test_a\n\n1 failed",
            meta: {
              job: {
                tool_use_id: "call-1",
                name: "bash",
                label: "pytest",
                exit_code: 1,
              },
            },
          },
        ],
      },
      { role: "assistant", content: [{ type: "text", text: "test_a fails" }] },
    ]);

    expect(renderMessages).toHaveLength(4);
    const [, launch, marker, reply] = renderMessages;
    expect(marker).toEqual({
      kind: "job-marker",
      sourceIndex: 4,
      renderKey: "jobs:4",
      jobs: [
        {
          tool_use_id: "call-1",
          label: "pytest",
          exit_code: 1,
          output: "FAILED test_a\n\n1 failed",
        },
      ],
    });
    // The wake reply is its own bubble with no work: nothing to fold.
    expect(expectChat(reply).content).toEqual([
      expect.objectContaining({ type: "text", text: "test_a fails" }),
    ]);
    const start = expectChat(launch).content[0];
    if (start?.type !== "tool_use") throw new Error("expected the start card");
    expect(start.runtime?.metadata).toEqual({
      background: true,
      pid: 42,
      log: "/tmp/job.log",
      exit_code: 1,
    });
    expect(start.runtime?.isError).toBe(false);
  });

  const backgroundStart: ChatMessage[] = [
    { role: "user", content: [{ type: "text", text: "run tests later" }] },
    {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "call-1",
          name: "bash",
          input: { command: "pytest", background: true },
        },
      ],
    },
    {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "call-1",
          output: "Started in background (pid 42): pytest\nLog: /tmp/job.log",
          metadata: { background: true, pid: 42, log: "/tmp/job.log" },
          is_error: false,
        },
      ],
    },
    { role: "assistant", content: [{ type: "text", text: "started" }] },
  ];
  const startCard = (rendered: RenderMessage[]) => {
    const block = expectChat(rendered[1]).content[0];
    if (block?.type !== "tool_use") throw new Error("expected the start card");
    return block;
  };

  it("marks the call of a still running background job as running", () => {
    const running = new Set(["call-1"]);
    const first = startCard(buildRenderMessages(backgroundStart, {}, running));
    const again = startCard(buildRenderMessages(backgroundStart, {}, running));

    expect(first.runtime?.metadata).toEqual({
      background: true,
      pid: 42,
      log: "/tmp/job.log",
      running: true,
    });
    expect(again.runtime?.metadata).toBe(first.runtime?.metadata);
    expect(
      startCard(buildRenderMessages(backgroundStart)).runtime?.metadata,
    ).not.toHaveProperty("running");
  });

  it("prefers the job's result over a stale running entry", () => {
    const finished: ChatMessage[] = [
      ...backgroundStart,
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "Background bash finished (pid 42, exit code -15): pytest\nLog: /tmp/job.log\n\n",
            meta: {
              job: {
                tool_use_id: "call-1",
                name: "bash",
                label: "pytest",
                exit_code: -15,
              },
            },
          },
        ],
      },
    ];

    const start = startCard(
      buildRenderMessages(finished, {}, new Set(["call-1"])),
    );

    expect(start.runtime?.metadata).toEqual({
      background: true,
      pid: 42,
      log: "/tmp/job.log",
      exit_code: -15,
    });
  });

  it("puts the job marker ahead of a steer's user bubble", () => {
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "build it" }] },
      { role: "assistant", content: [{ type: "text", text: "working" }] },
      {
        role: "user",
        content: [
          {
            type: "text",
            text: "Background bash finished (pid 7, exit code 0): make\nLog: /tmp/make.log\n\nok",
            meta: {
              job: {
                tool_use_id: "call-2",
                name: "bash",
                label: "make",
                exit_code: 0,
              },
            },
          },
          { type: "text", text: "also lint" },
        ],
        meta: { steer: true, input_ids: ["in-1"] },
      },
      { role: "assistant", content: [{ type: "text", text: "linting" }] },
    ]);

    expect(renderMessages).toHaveLength(5);
    const [, , marker] = renderMessages;
    const [, , , user, reply] = renderMessages.map((m) =>
      isChatMessage(m) ? m : null,
    );
    expect(marker).toMatchObject({
      kind: "job-marker",
      jobs: [
        { tool_use_id: "call-2", label: "make", exit_code: 0, output: "ok" },
      ],
    });
    expect(user?.role).toBe("user");
    expect(user?.content).toMatchObject([{ type: "text", text: "also lint" }]);
    expect(user?.content).toHaveLength(1);
    expect(user?.meta?.steer).toBe(true);
    expect(reply?.role).toBe("assistant");
    expect(reply?.content).toMatchObject([{ type: "text", text: "linting" }]);
  });

  it("wraps text attachments like CLI file references", () => {
    const message = createUserMessage("review this", [
      {
        id: "file-1",
        kind: "text",
        name: 'main <"v2">.py',
        text: 'print("ok")',
      },
    ]);

    expect(message).toEqual({
      role: "user",
      content: [
        { type: "text", text: "review this" },
        {
          type: "text",
          text: '<file name="main &lt;&quot;v2&quot;&gt;.py">\nprint("ok")\n</file>',
          meta: { attachment: true, path: 'main <"v2">.py' },
        },
      ],
    });
  });

  it("updates the latest thinking block duration", () => {
    const initial: ChatMessage[] = [
      {
        role: "assistant",
        content: [
          {
            type: "thinking",
            text: "plan",
            meta: { native: { signature: "sig" } },
          },
          { type: "text", text: "answer" },
        ],
      },
    ];

    const updated = updateLatestThinkingDuration(initial, 1200);

    expect(updated[0]?.content[0]).toEqual({
      type: "thinking",
      text: "plan",
      meta: { native: { signature: "sig" }, duration_ms: 1200 },
    });
  });
});

describe("run errors", () => {
  it("gives an error before any output its own assistant", () => {
    const user: ChatMessage = {
      role: "user",
      content: [{ type: "text", text: "hi" }],
    };
    const messages = markTailAssistantStopped([user], "error", "HTTP 401");

    expect(messages.at(-1)).toEqual({
      role: "assistant",
      content: [],
      meta: { stop_reason: "error", error: "HTTP 401" },
    });
    expect(markTailAssistantStopped([user], "cancelled")).toEqual([user]);

    // Kept when a later turn follows, as a reloaded session would be.
    const rendered = buildRenderMessages([...messages, user]);
    expect(expectChat(rendered[1]).meta?.error).toBe("HTTP 401");
  });
});

describe("turn interruption", () => {
  const user = (text: string): ChatMessage => ({
    role: "user",
    content: [{ type: "text", text }],
  });
  const toolCall: ChatMessage = {
    role: "assistant",
    content: [{ type: "tool_use", id: "t1", name: "read", input: {} }],
    meta: { stop_reason: "tool_use" },
  };
  const toolResult: ChatMessage = {
    role: "user",
    content: [
      {
        type: "tool_result",
        tool_use_id: "t1",
        output: "ok",
        metadata: null,
        is_error: false,
      },
    ],
  };
  const reply = (stop_reason: string): ChatMessage => ({
    role: "assistant",
    content: [{ type: "text", text: "reply" }],
    meta: { stop_reason },
  });
  const interruptions = (messages: ChatMessage[]) =>
    buildRenderMessages(messages)
      .map(expectChat)
      .filter((message) => message.role === "assistant")
      .map((message) => message.interruption);

  it("reads how each turn ended from its last record", () => {
    expect(
      interruptions([
        user("done"),
        toolCall,
        toolResult,
        reply("stop"),
        user("partial"),
        reply("cancelled"),
        user("failed"),
        toolCall,
        toolResult,
        reply("error"),
      ]),
    ).toEqual([undefined, "cancelled", "error"]);
  });

  it("treats a turn that ends on tool results as stopped", () => {
    // A cancel between rounds persists no assistant record.
    expect(
      interruptions([user("go"), toolCall, toolResult, user("next")]),
    ).toEqual(["cancelled"]);
  });

  it("reads tool results followed by a steer as a continued turn", () => {
    const at = (seconds: number) =>
      new Date(Date.UTC(2026, 0, 1, 0, 0, seconds)).toISOString();
    const steer: ChatMessage = {
      ...user("use sqlite"),
      meta: { created_at: at(14), steer: true, input_ids: ["c1"] },
    };
    const rendered = buildRenderMessages([
      { ...user("go"), meta: { created_at: at(0) } },
      {
        ...toolCall,
        meta: {
          ...toolCall.meta,
          created_at: at(2),
          usage: { total_tokens: 100, input_tokens: 90, output_tokens: 10 },
        },
      },
      toolResult,
      steer,
      reply("stop"),
    ]).map(expectChat);

    expect(rendered.map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    // The segment before the steer reads like a finished turn and runs to
    // the steer message, so the tool time after its last request counts.
    expect(rendered[1]?.interruption).toBeUndefined();
    expect(rendered[1]?.stats?.duration_ms).toBe(14000);
    expect(rendered[2]?.content).toMatchObject([
      { type: "text", text: "use sqlite" },
    ]);
    expect(rendered[3]?.interruption).toBeUndefined();
  });
});

describe("turn stats", () => {
  it("sums per-request usage and cost across a history tool loop", () => {
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "go" }] },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "bash", input: {} }],
        meta: {
          model: "m",
          context_window: 100_000,
          usage: { total_tokens: 1_000, input_tokens: 900, output_tokens: 100 },
          cost: { input: 0.006, output: 0.004, total: 0.01 },
        },
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t1",
            output: "ok",
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: {
          model: "m",
          context_window: 100_000,
          usage: {
            total_tokens: 2_000,
            input_tokens: 1_500,
            output_tokens: 500,
            cache_read_tokens: 800,
          },
          cost: {
            input: 0.01,
            cache_read: 0.005,
            output: 0.005,
            total: 0.02,
          },
        },
      },
    ]);

    expect(renderMessages).toHaveLength(2);
    const stats = expectChat(renderMessages[1]).stats;
    expect(stats).toMatchObject({
      total_tokens: 3_000,
      input_tokens: 2_400,
      output_tokens: 600,
      cache_read_tokens: 800,
      // Context occupancy is the last request's total, not a sum.
      context_tokens: 2_000,
      context_window: 100_000,
    });
    expect(stats?.cost).toEqual({
      input: expect.closeTo(0.016),
      cache_read: expect.closeTo(0.005),
      output: expect.closeTo(0.009),
      total: expect.closeTo(0.03),
    });
  });

  it("skips missing costs and downgrades mixed detail to a known total", () => {
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "go" }] },
      // Cancelled partial: no usage at all — contributes nothing.
      {
        role: "assistant",
        content: [{ type: "text", text: "partial" }],
        meta: { model: "m" },
      },
      { role: "user", content: [{ type: "text", text: "again" }] },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "bash", input: {} }],
        meta: {
          usage: { total_tokens: 1_000, input_tokens: 900, output_tokens: 100 },
          cost: { input: 0.006, output: 0.004, total: 0.01 },
        },
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t1",
            output: "ok",
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t2", name: "bash", input: {} }],
        meta: {
          usage: {
            total_tokens: 2_000,
            input_tokens: 1_800,
            output_tokens: 200,
          },
        },
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t2",
            output: "ok",
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: {
          usage: {
            total_tokens: 3_000,
            input_tokens: 2_700,
            output_tokens: 300,
          },
          cost: { total: 0.02 },
        },
      },
    ]);

    expect(expectChat(renderMessages[1]).stats).toBeUndefined();
    expect(expectChat(renderMessages[3]).stats).toEqual({
      total_tokens: 6_000,
      input_tokens: 5_400,
      output_tokens: 600,
      context_tokens: 3_000,
      cost: { total: 0.03 },
    });
  });

  it("keeps cost-only requests in an automatically compacted turn", () => {
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "go" }] },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "bash", input: {} }],
        meta: { cost: { total: 0.01 } },
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t1",
            output: "ok",
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: "compact",
        content: [],
        meta: { trigger: "auto", cost: { total: 0.005 } },
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: {
          usage: { total_tokens: 500, input_tokens: 450, output_tokens: 50 },
          cost: { input: 0.0015, output: 0.0005, total: 0.002 },
        },
      },
    ]);

    expect(renderMessages).toHaveLength(2);
    expect(expectChat(renderMessages[1]).stats).toEqual({
      total_tokens: 500,
      input_tokens: 450,
      output_tokens: 50,
      context_tokens: 500,
      cost: { total: 0.017 },
    });
  });

  it("takes the latest cumulative values for a streaming turn instead of summing", () => {
    // SSE usage events patch turn-cumulative values onto each raw assistant
    // of the tool loop; summing them would double-count the earlier requests.
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "go" }] },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "bash", input: {} }],
        meta: {
          context_tokens: 1_000,
          context_window: 100_000,
          turn_usage: {
            total_tokens: 1_000,
            input_tokens: 900,
            output_tokens: 100,
          },
          turn_cost: { input: 0.006, output: 0.004, total: 0.01 },
        },
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t1",
            output: "ok",
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: {
          context_tokens: 2_000,
          context_window: 100_000,
          turn_usage: {
            total_tokens: 3_000,
            input_tokens: 2_400,
            output_tokens: 600,
          },
          turn_cost: { input: 0.016, output: 0.014, total: 0.03 },
        },
      },
    ]);

    expect(expectChat(renderMessages[1]).stats).toEqual({
      total_tokens: 3_000,
      input_tokens: 2_400,
      output_tokens: 600,
      context_tokens: 2_000,
      context_window: 100_000,
      cost: { input: 0.016, output: 0.014, total: 0.03 },
    });
  });

  it("folds an automatic compaction into its turn's work and totals", () => {
    const renderMessages = buildRenderMessages([
      { role: "user", content: [{ type: "text", text: "go" }] },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "bash", input: {} }],
        meta: {
          context_window: 100_000,
          usage: {
            total_tokens: 1_000,
            input_tokens: 900,
            output_tokens: 100,
          },
          cost: { input: 0.008, output: 0.002, total: 0.01 },
        },
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "t1",
            output: "ok",
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: "compact",
        content: [],
        meta: {
          trigger: "auto",
          context_window: 100_000,
          usage: {
            total_tokens: 1_200,
            input_tokens: 1_100,
            output_tokens: 100,
          },
          cost: { input: 0.004, output: 0.001, total: 0.005 },
        },
      },
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: {
          context_window: 100_000,
          usage: {
            total_tokens: 500,
            input_tokens: 450,
            output_tokens: 50,
          },
          cost: { input: 0.0015, output: 0.0005, total: 0.002 },
        },
      },
    ]);

    expect(renderMessages).toHaveLength(2);
    const turn = expectChat(renderMessages[1]);
    expect(turn.content.map((block) => block.type)).toEqual([
      "tool_use",
      "compact",
      "text",
    ]);
    expect(turn.stats).toEqual({
      total_tokens: 2_700,
      input_tokens: 2_450,
      output_tokens: 250,
      context_tokens: 500,
      context_window: 100_000,
      cost: { input: 0.0135, output: 0.0035, total: 0.017 },
    });
  });
});

describe("turn duration", () => {
  const at = (seconds: number) =>
    new Date(Date.UTC(2026, 0, 1, 0, 0, seconds)).toISOString();
  const toolResult = (id: string, created_at: string): ChatMessage => ({
    role: "user",
    content: [
      {
        type: "tool_result",
        tool_use_id: id,
        output: "ok",
        metadata: null,
        is_error: false,
      },
    ],
    meta: { created_at },
  });

  it("ends a history turn at its last completed response or auto compaction", () => {
    const renderMessages = buildRenderMessages([
      {
        role: "user",
        content: [{ type: "text", text: "one" }],
        meta: { created_at: at(0) },
      },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "read", input: {} }],
        meta: { created_at: at(5), stop_reason: "tool_use" },
      },
      toolResult("t1", at(6)),
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: { created_at: at(12), stop_reason: "stop" },
      },
      {
        role: "compact",
        content: [],
        meta: { created_at: at(15), trigger: "auto" },
      },
      {
        role: "user",
        content: [{ type: "text", text: "two" }],
        meta: { created_at: at(20) },
      },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t2", name: "read", input: {} }],
        meta: { created_at: at(22), stop_reason: "tool_use" },
      },
      toolResult("t2", at(30)),
      {
        role: "assistant",
        content: [{ type: "text", text: "partial" }],
        meta: { created_at: at(40), stop_reason: "cancelled" },
      },
    ]);

    // The SDK sends no usage event for a partial response or a tool result.
    expect(expectChat(renderMessages[1]).stats?.duration_ms).toBe(15_000);
    expect(expectChat(renderMessages[3]).stats?.duration_ms).toBe(2_000);
  });

  it("keeps the streamed duration of a reattached run over its history", () => {
    const renderMessages = buildRenderMessages([
      {
        role: "user",
        content: [{ type: "text", text: "go" }],
        meta: { created_at: at(0) },
      },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "read", input: {} }],
        meta: { created_at: at(5), stop_reason: "tool_use" },
      },
      toolResult("t1", at(6)),
      {
        role: "assistant",
        content: [{ type: "text", text: "done" }],
        meta: { turn_usage: { total_tokens: 10 }, turn_duration_ms: 9_000 },
      },
    ]);

    expect(expectChat(renderMessages[1]).stats?.duration_ms).toBe(9_000);
  });
});

describe("splitTurn", () => {
  it("keeps interim text in the work and splits off a compaction after the answer", () => {
    const thinking = { type: "thinking" as const, text: "plan" };
    const interim = { type: "text" as const, text: "Let me look." };
    const tool = {
      type: "tool_use" as const,
      id: "t1",
      name: "read",
      input: {},
    };
    const midCompact = { type: "compact" as const, renderKey: "c1" };
    const answer = { type: "text" as const, text: "Done." };
    const lastCompact = { type: "compact" as const, renderKey: "c2" };

    expect(
      splitTurn([thinking, interim, tool, midCompact, answer, lastCompact]),
    ).toEqual({
      work: [thinking, interim, tool, midCompact],
      answer: [answer],
      afterAnswer: [lastCompact],
    });
  });
});
