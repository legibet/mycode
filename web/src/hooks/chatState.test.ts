import { describe, expect, it } from "vitest";

import { resolveInitialSessionId } from "@/hooks/chatState";

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
