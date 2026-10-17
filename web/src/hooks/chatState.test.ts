import { describe, expect, it } from "vitest";

import { chatReducer, INITIAL_CHAT_STATE, resolveInitialSessionId } from "@/hooks/chatState";

describe("resolveInitialSessionId", () => {
  it("prefers the previously active session", () => {
    const sessions = [{ id: "latest" }, { id: "previous" }];

    expect(resolveInitialSessionId(sessions, "previous")).toBe("previous");
  });

  it("falls back to the latest session", () => {
    const sessions = [{ id: "latest" }, { id: "older" }];

    expect(resolveInitialSessionId(sessions, "missing")).toBe("latest");
    expect(resolveInitialSessionId(sessions, "")).toBe("latest");
    expect(resolveInitialSessionId([], "missing")).toBeNull();
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
