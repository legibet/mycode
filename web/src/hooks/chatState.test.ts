import { describe, expect, it } from "vitest";

import type { RunEvent } from "@/api";
import { chatReducer, INITIAL_CHAT_STATE, resolveInitialSession } from "@/hooks/chatState";

describe("resolveInitialSession", () => {
  it("prefers the previously active session", () => {
    const sessions = [{ id: "latest" }, { id: "previous" }];

    expect(resolveInitialSession(sessions, "previous")).toBe(sessions[1]);
  });

  it("falls back to the latest session", () => {
    const sessions = [{ id: "latest" }, { id: "older" }];

    expect(resolveInitialSession(sessions, "missing")).toBe(sessions[0]);
    expect(resolveInitialSession(sessions, "")).toBe(sessions[0]);
    expect(resolveInitialSession([], "missing")).toBeNull();
  });
});

describe("chatReducer", () => {
  it.each(["error", "cancelled"] as const)(
    "drops waiting permissions when the run ends with %s",
    (type) => {
      const waiting = chatReducer(INITIAL_CHAT_STATE, {
        type: "apply_event",
        event: {
          type: "permission_request",
          request_id: "req-1",
          tool_use_id: "call-1",
          tool_name: "bash",
          preview: "ls",
        },
      });
      expect(waiting.permissions).toHaveLength(1);

      const ended = chatReducer(waiting, {
        type: "apply_event",
        event: type === "error" ? { type, message: "boom" } : { type },
      });

      expect(ended.permissions).toEqual([]);
    },
  );

  it("keeps the subagent a permission request names", () => {
    const state = chatReducer(INITIAL_CHAT_STATE, {
      type: "apply_event",
      event: {
        type: "permission_request",
        request_id: "req-1",
        tool_use_id: "call-1",
        tool_name: "bash",
        preview: "rm -rf build",
        agent: "Clean the build",
      },
    });

    expect(state.permissions).toEqual([
      {
        request_id: "req-1",
        tool_use_id: "call-1",
        tool_name: "bash",
        preview: "rm -rf build",
        agent: "Clean the build",
      },
    ]);
  });

  describe("a running tool's metadata", () => {
    const named = { agent: "general", session_id: "child-a", background: false };
    const agentStart = {
      type: "tool_start" as const,
      tool_call: { id: "call-a", name: "agent", input: { description: "Map", prompt: "…" } },
    };
    const replay = (replayEvents: RunEvent[]) =>
      chatReducer(INITIAL_CHAT_STATE, {
        type: "set_messages",
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
        sessionId: "s1",
        replayEvents,
      });

    it("survives a reload: replayed output keeps the metadata later text-only output does not repeat", () => {
      const state = replay([
        agentStart,
        { type: "tool_output", tool_use_id: "call-a", output: "", metadata: named },
        { type: "tool_output", tool_use_id: "call-a", output: "Read a.py\n" },
      ]);

      expect(state.toolRuntimeById["call-a"]).toMatchObject({
        pending: true,
        output: "Read a.py\n",
        metadata: named,
      });
    });

    it("is ignored when the call's start was evicted from the replay", () => {
      const state = replay([
        { type: "tool_output", tool_use_id: "call-a", output: "", metadata: named },
      ]);

      expect(state.rawMessages.some((message) => message.role === "assistant")).toBe(false);
    });
  });

  describe("a blocking agent's spend", () => {
    const withAgentCall = chatReducer(
      chatReducer(INITIAL_CHAT_STATE, {
        type: "set_messages",
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
        sessionId: "s1",
        sessionUsage: { input_tokens: 100, output_tokens: 10, cost: { total: 0.1 } },
      }),
      {
        type: "apply_event",
        event: {
          type: "tool_start",
          tool_call: { id: "call-a", name: "agent", input: { description: "Map", prompt: "…" } },
        },
      },
    );
    const agentDone = {
      type: "apply_event" as const,
      event: {
        type: "tool_done" as const,
        tool_use_id: "call-a",
        output: "done",
        is_error: false,
        metadata: {
          agent: "general",
          session_id: "child-a",
          status: "completed",
          usage: { input_tokens: 40, output_tokens: 4 },
          cost: { total: 0.02 },
        },
      },
    };
    // The server's totals once they include the child.
    const usage = {
      type: "apply_event" as const,
      event: {
        type: "usage" as const,
        session_usage: { input_tokens: 150, output_tokens: 15 },
        session_cost: { total: 0.13 },
      },
    };

    it("adds to the session totals when the call returns", () => {
      const state = chatReducer(withAgentCall, agentDone);

      expect(state.sessionUsage).toMatchObject({ input_tokens: 140, output_tokens: 14 });
      expect(state.sessionUsage?.cost?.total).toBeCloseTo(0.12);
    });

    it("is replaced, not added twice, by a later usage event's totals", () => {
      const state = chatReducer(chatReducer(withAgentCall, agentDone), usage);

      expect(state.sessionUsage).toEqual({
        input_tokens: 150,
        output_tokens: 15,
        cost: { total: 0.13 },
      });
    });

    it("adds to totals a usage event set before the call returned", () => {
      const before = {
        ...usage,
        event: {
          ...usage.event,
          session_usage: { input_tokens: 110, output_tokens: 11 },
          session_cost: { total: 0.11 },
        },
      };
      const state = chatReducer(chatReducer(withAgentCall, before), agentDone);

      expect(state.sessionUsage).toMatchObject({ input_tokens: 150, output_tokens: 15 });
      expect(state.sessionUsage?.cost?.total).toBeCloseTo(0.13);
    });

    it("keeps a snapshot's totals when it replays the call and an earlier usage event", () => {
      const state = chatReducer(INITIAL_CHAT_STATE, {
        type: "set_messages",
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
        sessionId: "s1",
        sessionUsage: { input_tokens: 150, output_tokens: 15, cost: { total: 0.13 } },
        replayEvents: [
          {
            type: "tool_start",
            tool_call: { id: "call-a", name: "agent", input: { description: "Map", prompt: "…" } },
          },
          // Sent before the child's spend joined the server's totals.
          {
            type: "usage",
            session_usage: { input_tokens: 100, output_tokens: 10 },
            session_cost: { total: 0.1 },
            turn_cost: { total: 0.1 },
          },
          agentDone.event,
        ],
      });

      const assistant = state.rawMessages.findLast((message) => message.role === "assistant");
      expect(assistant?.meta?.turn_cost).toEqual({ total: 0.1 });
      expect(state.sessionUsage).toEqual({
        input_tokens: 150,
        output_tokens: 15,
        cost: { total: 0.13 },
      });
    });

    it("leaves the totals alone for other tools", () => {
      const bash = chatReducer(withAgentCall, {
        type: "apply_event",
        event: { type: "tool_start", tool_call: { id: "call-b", name: "bash", input: {} } },
      });
      const state = chatReducer(bash, {
        ...agentDone,
        event: { ...agentDone.event, tool_use_id: "call-b" },
      });

      expect(state.sessionUsage).toBe(bash.sessionUsage);
    });
  });
});
