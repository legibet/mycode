import { describe, expect, it } from "vitest";

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
});
