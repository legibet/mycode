import { describe, expect, it } from "vitest";

import type { ChatMessage, RenderMessage } from "../types";
import { isCompactMarker } from "../types";
import {
  buildRenderMessages,
  createUserMessage,
  updateLatestThinkingDuration,
} from "./messages";

function expectChat(message: RenderMessage | undefined): ChatMessage {
  if (!message || isCompactMarker(message)) {
    throw new Error("expected a ChatMessage, got compact marker or undefined");
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

  it("keeps cost-only requests in a compacted history turn", () => {
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
        meta: { cost: { total: 0.005 } },
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

    expect(expectChat(renderMessages[3]).stats).toEqual({
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

  it("shows a compacted tool turn's totals on its final reply", () => {
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

    expect(expectChat(renderMessages[1]).stats).toBeUndefined();
    expect(expectChat(renderMessages[3]).stats).toEqual({
      total_tokens: 2_700,
      input_tokens: 2_450,
      output_tokens: 250,
      context_tokens: 500,
      context_window: 100_000,
      cost: { input: 0.0135, output: 0.0035, total: 0.017 },
    });
  });
});
