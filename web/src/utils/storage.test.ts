import { beforeEach, describe, expect, it } from "vitest";

import {
  loadActiveSession,
  loadConfig,
  removeActiveSession,
  saveActiveSession,
} from "./storage";

function createLocalStorage() {
  const store = new Map();

  return {
    get length() {
      return store.size;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    getItem(key: string) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key: string, value: string) {
      store.set(key, String(value));
    },
    removeItem(key: string) {
      store.delete(key);
    },
    clear() {
      store.clear();
    },
  };
}

describe("storage", () => {
  beforeEach(() => {
    globalThis.localStorage = createLocalStorage();
  });

  it("stores active sessions per workspace", () => {
    saveActiveSession("/workspace/a", "session-a");
    saveActiveSession("/workspace/b", "session-b");

    expect(loadActiveSession("/workspace/a")).toBe("session-a");
    expect(loadActiveSession("/workspace/b")).toBe("session-b");
  });

  it("returns empty for workspaces without a saved session", () => {
    saveActiveSession("/workspace/a", "session-a");

    expect(loadActiveSession("/workspace/b")).toBe("");
  });

  it("removes the saved active session for a workspace", () => {
    saveActiveSession("/workspace/a", "session-a");
    saveActiveSession("/workspace/b", "session-b");

    removeActiveSession("/workspace/a");

    expect(loadActiveSession("/workspace/a")).toBe("");
    expect(loadActiveSession("/workspace/b")).toBe("session-b");
  });

  it("keeps stable config fields when dropping the old effort value", () => {
    localStorage.setItem(
      "mycode_config",
      JSON.stringify({
        _v: 1,
        provider: "anthropic",
        model: "claude-sonnet-4-6",
        cwd: "/workspace",
        reasoningEffort: "high",
      }),
    );

    expect(loadConfig()).toEqual({
      provider: "anthropic",
      model: "claude-sonnet-4-6",
      cwd: "/workspace",
      reasoningEfforts: {},
    });
  });
});
