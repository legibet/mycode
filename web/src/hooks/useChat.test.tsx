import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage, RenderMessage } from "../types";
import { isCompactMarker } from "../types";
import { loadActiveSession, saveActiveSession } from "../utils/storage";
import { useChat } from "./useChat";

function expectChat(message: RenderMessage | undefined): ChatMessage {
  if (!message || isCompactMarker(message)) {
    throw new Error("expected a ChatMessage, got compact marker or undefined");
  }
  return message;
}

function createLocalStorage() {
  const store = new Map<string, string>();

  return {
    get length() {
      return store.size;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    getItem(key: string) {
      return store.has(key) ? (store.get(key) ?? null) : null;
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

function createJsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderChatHook(
  overrides?: Partial<Parameters<typeof useChat>[0]>,
  remoteConfig?: Parameters<typeof useChat>[1],
) {
  return renderHook(() =>
    useChat(
      {
        provider: "",
        model: "",
        cwd: "/workspace/a",
        reasoningEfforts: {},
        ...overrides,
      },
      remoteConfig,
    ),
  );
}

type MockResponse =
  | Response
  | ((init?: RequestInit) => Promise<Response> | Response);

function mockFetch(routes: Record<string, MockResponse>) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      for (const [prefix, response] of Object.entries(routes)) {
        if (!url.startsWith(prefix)) continue;
        if (typeof response === "function") {
          return response(init);
        }
        return response.clone();
      }
      throw new Error(`Unexpected fetch: ${url}`);
    },
  );
  globalThis.fetch = fetchMock as typeof fetch;
  return fetchMock;
}

describe("useChat", () => {
  beforeEach(() => {
    globalThis.localStorage = createLocalStorage();
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({ sessions: [] }),
    });
  });

  it("creates a draft session when the workspace has no saved sessions", async () => {
    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.sessions).toHaveLength(1);
    });

    expect(result.current.activeSession.isDraft).toBe(true);
    expect(result.current.sessions[0]?.id).toBe(
      result.current.activeSession.id,
    );
    expect(result.current.messages).toEqual([]);
    expect(result.current.loading).toBe(false);
  });

  it("sends auto as an explicit reasoning effort override", async () => {
    const fetchMock = mockFetch({
      "/api/sessions?cwd=": createJsonResponse({ sessions: [] }),
      "/api/chat": createJsonResponse({
        run: {
          id: "run-auto",
          session_id: "draft-auto",
          kind: "chat",
          status: "running",
          last_seq: 0,
        },
        session: { id: "draft-auto", title: "Draft" },
      }),
      "/api/runs/run-auto/stream?after=0": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });
    const { result } = renderChatHook(
      {
        provider: "anthropic",
        model: "claude-sonnet-5",
        reasoningEfforts: { "anthropic/claude-sonnet-5": "auto" },
      },
      {
        providers: {
          anthropic: {
            name: "anthropic",
            provider: "anthropic",
            type: "anthropic",
            models: ["claude-sonnet-5"],
            base_url: "",
            has_api_key: true,
            supports_reasoning_effort: true,
            reasoning_efforts: { "claude-sonnet-5": ["low"] },
          },
        },
      },
    );
    await waitFor(() => expect(result.current.sessionLoading).toBe(false));

    await result.current.send({ text: "hello", workspaceFiles: [] });

    const chatCall = fetchMock.mock.calls.find(([url]) => url === "/api/chat");
    expect(JSON.parse(String(chatCall?.[1]?.body)).reasoning_effort).toBe(
      "auto",
    );
  });

  it("sends text attachments as attachment text blocks", async () => {
    const fetchMock = mockFetch({
      "/api/sessions?cwd=": createJsonResponse({ sessions: [] }),
      "/api/chat": createJsonResponse({
        run: {
          id: "run-1",
          session_id: "draft-1",
          kind: "chat",
          status: "running",
          last_seq: 0,
        },
        session: { id: "draft-1", title: "Draft" },
      }),
      "/api/runs/run-1/stream?after=0": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });

    const { result } = renderChatHook({
      provider: "anthropic",
      model: "claude-sonnet-4-6",
    });

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
    });

    await result.current.send({ text: "check this", workspaceFiles: [] }, [
      { id: "file-1", kind: "text", name: "main.py", text: 'print("ok")' },
    ]);

    const chatCall = fetchMock.mock.calls.find(([url]) => url === "/api/chat");
    expect(chatCall).toBeTruthy();
    expect(JSON.parse(String(chatCall?.[1]?.body))).toEqual({
      session_id: result.current.activeSession.id,
      provider: "anthropic",
      model: "claude-sonnet-4-6",
      cwd: "/workspace/a",
      input: [
        { type: "text", text: "check this" },
        {
          type: "text",
          text: 'print("ok")',
          name: "main.py",
          is_attachment: true,
        },
      ],
    });
  });

  it("sends inline workspace references as path blocks, deduplicated", async () => {
    const fetchMock = mockFetch({
      "/api/sessions?cwd=": createJsonResponse({ sessions: [] }),
      "/api/chat": createJsonResponse({
        run: {
          id: "run-1",
          session_id: "draft-1",
          kind: "chat",
          status: "running",
          last_seq: 0,
        },
        session: { id: "draft-1", title: "Draft" },
      }),
      "/api/runs/run-1/stream?after=0": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });

    const { result } = renderChatHook({
      provider: "anthropic",
      model: "claude-sonnet-4-6",
    });

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
    });

    await result.current.send({
      text: "compare @src/a.ts and @src/a.ts with @logo.png",
      workspaceFiles: [
        { path: "src/a.ts", name: "a.ts", kind: "text" },
        { path: "src/a.ts", name: "a.ts", kind: "text" },
        { path: "logo.png", name: "logo.png", kind: "image" },
      ],
    });

    const chatCall = fetchMock.mock.calls.find(([url]) => url === "/api/chat");
    expect(JSON.parse(String(chatCall?.[1]?.body)).input).toEqual([
      { type: "text", text: "compare @src/a.ts and @src/a.ts with @logo.png" },
      { type: "text", path: "src/a.ts", name: "src/a.ts", is_attachment: true },
      {
        type: "image",
        path: "logo.png",
        name: "logo.png",
        is_attachment: true,
      },
    ]);
  });

  it("sends document data without keeping it in UI messages", async () => {
    const fetchMock = mockFetch({
      "/api/sessions?cwd=": createJsonResponse({ sessions: [] }),
      "/api/chat": createJsonResponse({
        run: {
          id: "run-1",
          session_id: "draft-1",
          kind: "chat",
          status: "running",
          last_seq: 0,
        },
        session: { id: "draft-1", title: "Draft" },
      }),
      "/api/runs/run-1/stream?after=0": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });

    const { result } = renderChatHook({
      provider: "xiaomi",
      model: "mimo-v2.5-pro",
    });

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
    });

    await result.current.send({ text: "summarize", workspaceFiles: [] }, [
      {
        id: "file-1",
        kind: "document",
        name: "report.pdf",
        mime_type: "application/pdf",
        data: "large-pdf-base64",
      },
    ]);

    const chatCall = fetchMock.mock.calls.find(([url]) => url === "/api/chat");
    expect(JSON.parse(String(chatCall?.[1]?.body)).input[1]).toEqual({
      type: "document",
      data: "large-pdf-base64",
      mime_type: "application/pdf",
      name: "report.pdf",
    });

    await waitFor(() => {
      const userMessage = result.current.messages.find(
        (message): message is ChatMessage =>
          !isCompactMarker(message) && message.role === "user",
      );
      expect(userMessage?.content[1]).toEqual({
        type: "document",
        data: "",
        mime_type: "application/pdf",
        name: "report.pdf",
        renderKey: "user:0:1",
      });
    });
  });

  it("keeps assistant replies when loading a persisted session with text attachments", async () => {
    globalThis.localStorage.setItem(
      "mycode.activeSessions",
      JSON.stringify({ "/workspace/a": "session-1" }),
    );
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-1", title: "Persisted" }],
      }),
      "/api/sessions/session-1": createJsonResponse({
        session: { id: "session-1", title: "Persisted" },
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: "check this" },
              {
                type: "text",
                text: '<file name="main.py">\nprint("ok")\n</file>',
                meta: { attachment: true, path: "main.py" },
              },
            ],
          },
          {
            role: "assistant",
            content: [{ type: "text", text: "looks good" }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.messages).toHaveLength(2);
    });

    expect(result.current.messages[1]).toEqual({
      role: "assistant",
      renderKey: "assistant:1",
      sourceIndex: 1,
      content: [
        {
          type: "text",
          text: "looks good",
          renderKey: "assistant:1:0",
        },
      ],
    });
  });

  it("rebuilds assistant replies from active-run pending events after refresh", async () => {
    globalThis.localStorage.setItem(
      "mycode.activeSessions",
      JSON.stringify({ "/workspace/a": "session-2" }),
    );
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-2", title: "Running" }],
      }),
      "/api/sessions/session-2": createJsonResponse({
        session: { id: "session-2", title: "Running" },
        messages: [
          {
            role: "user",
            content: [
              {
                type: "text",
                text: '<file name="main.py">\nprint("ok")\n</file>',
                meta: { attachment: true, path: "main.py" },
              },
            ],
          },
        ],
        active_run: {
          id: "run-2",
          session_id: "session-2",
          kind: "chat",
          status: "running",
          last_seq: 1,
        },
        pending_events: [{ type: "text", delta: "looks good", seq: 1 }],
      }),
      "/api/runs/run-2/stream?after=1": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.messages).toHaveLength(2);
    });

    expect(expectChat(result.current.messages[1]).content[0]).toEqual({
      type: "text",
      text: "looks good",
      renderKey: "assistant:1:0",
    });
  });

  it("appends all tool output deltas without inserting separators", async () => {
    const largeDelta = "x".repeat(110 * 1024);
    globalThis.localStorage.setItem(
      "mycode.activeSessions",
      JSON.stringify({ "/workspace/a": "session-2" }),
    );
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-2", title: "Running" }],
      }),
      "/api/sessions/session-2": createJsonResponse({
        session: { id: "session-2", title: "Running" },
        messages: [{ role: "user", content: [{ type: "text", text: "run" }] }],
        active_run: {
          id: "run-2",
          session_id: "session-2",
          kind: "chat",
          status: "running",
          last_seq: 3,
        },
        pending_events: [
          {
            type: "tool_start",
            tool_call: { id: "call-1", name: "bash", input: {} },
            seq: 1,
          },
          {
            type: "tool_output",
            tool_use_id: "call-1",
            output: "one\nsec",
            seq: 2,
          },
          {
            type: "tool_output",
            tool_use_id: "call-1",
            output: `ond\n${largeDelta}`,
            seq: 3,
          },
        ],
      }),
      "/api/runs/run-2/stream?after=3": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.messages).toHaveLength(2);
    });
    const block = expectChat(result.current.messages[1]).content[0];
    if (block?.type !== "tool_use") {
      throw new Error("expected a tool use block");
    }
    expect(block.runtime?.output).toBe(`one\nsecond\n${largeDelta}`);
  });

  it("uses the latest cumulative usage and clears unknown context", async () => {
    globalThis.localStorage.setItem(
      "mycode.activeSessions",
      JSON.stringify({ "/workspace/a": "session-2" }),
    );
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-2", title: "Running" }],
      }),
      "/api/sessions/session-2": createJsonResponse({
        session: { id: "session-2", title: "Running" },
        messages: [
          { role: "user", content: [{ type: "text", text: "before" }] },
          {
            role: "assistant",
            content: [{ type: "text", text: "previous" }],
            meta: {
              context_window: 100_000,
              usage: { total_tokens: 500 },
            },
          },
          { role: "user", content: [{ type: "text", text: "go" }] },
        ],
        session_cost: 0.4,
        active_run: {
          id: "run-2",
          session_id: "session-2",
          kind: "chat",
          status: "running",
          last_seq: 3,
        },
        pending_events: [
          { type: "text", delta: "hi", seq: 1 },
          {
            type: "usage",
            context_tokens: 1_000,
            context_window: 100_000,
            turn_usage: { input_tokens: 900, output_tokens: 100 },
            turn_cost: { input: 0.006, output: 0.004, total: 0.01 },
            session_cost: 0.41,
            seq: 2,
          },
          {
            type: "usage",
            context_window: 100_000,
            turn_usage: { input_tokens: 1_800, output_tokens: 200 },
            seq: 3,
          },
        ],
      }),
      "/api/runs/run-2/stream?after=3": new Response("data: [DONE]\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.messages).toHaveLength(4);
    });

    expect(result.current.sessionCost).toBeNull();
    expect(result.current.currentContext).toBeNull();
    expect(expectChat(result.current.messages[3]).stats).toEqual({
      input_tokens: 1_800,
      output_tokens: 200,
      context_window: 100_000,
    });
  });

  it("updates the active session immediately while loading selected history", async () => {
    saveActiveSession("/workspace/a", "session-1");

    let resolveSession2!: (response: Response) => void;
    const session2Response = new Promise<Response>((resolve) => {
      resolveSession2 = resolve;
    });

    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [
          { id: "session-2", title: "Second" },
          { id: "session-1", title: "First" },
        ],
      }),
      "/api/sessions/session-1": createJsonResponse({
        session: { id: "session-1", title: "First" },
        messages: [
          {
            role: "assistant",
            content: [{ type: "text", text: "first session" }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
      "/api/sessions/session-2": () => session2Response,
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-1");
    });

    let selectPromise!: Promise<void>;
    act(() => {
      selectPromise = result.current.selectSession("session-2");
    });

    expect(result.current.activeSession.id).toBe("session-2");
    expect(result.current.sessionLoading).toBe(true);
    expect(result.current.messages).toEqual([]);
    expect(result.current.messageSessionId).toBe("session-2");

    await act(async () => {
      resolveSession2(
        createJsonResponse({
          session: { id: "session-2", title: "Second" },
          messages: [
            {
              role: "assistant",
              content: [{ type: "text", text: "second session" }],
            },
          ],
          active_run: null,
          pending_events: [],
        }),
      );
      await selectPromise;
    });

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.messages).toHaveLength(1);
      expect(result.current.messageSessionId).toBe("session-2");
    });
    expect(expectChat(result.current.messages[0]).content[0]).toEqual({
      type: "text",
      text: "second session",
      renderKey: "assistant:0:0",
    });
  });

  it("ignores stale selected history when switching sessions again", async () => {
    saveActiveSession("/workspace/a", "session-1");

    let resolveSession2!: (response: Response) => void;
    const session2Response = new Promise<Response>((resolve) => {
      resolveSession2 = resolve;
    });
    let resolveSession3!: (response: Response) => void;
    const session3Response = new Promise<Response>((resolve) => {
      resolveSession3 = resolve;
    });

    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [
          { id: "session-3", title: "Third" },
          { id: "session-2", title: "Second" },
          { id: "session-1", title: "First" },
        ],
      }),
      "/api/sessions/session-1": createJsonResponse({
        session: { id: "session-1", title: "First" },
        messages: [
          {
            role: "assistant",
            content: [{ type: "text", text: "first session" }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
      "/api/sessions/session-2": () => session2Response,
      "/api/sessions/session-3": () => session3Response,
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-1");
    });

    let selectSession2!: Promise<void>;
    act(() => {
      selectSession2 = result.current.selectSession("session-2");
    });

    expect(result.current.activeSession.id).toBe("session-2");
    expect(result.current.messages).toEqual([]);
    expect(result.current.messageSessionId).toBe("session-2");

    let selectSession3!: Promise<void>;
    act(() => {
      selectSession3 = result.current.selectSession("session-3");
    });

    expect(result.current.activeSession.id).toBe("session-3");
    expect(result.current.messages).toEqual([]);
    expect(result.current.messageSessionId).toBe("session-3");

    await act(async () => {
      resolveSession2(
        createJsonResponse({
          session: { id: "session-2", title: "Second" },
          messages: [
            {
              role: "assistant",
              content: [{ type: "text", text: "second session" }],
            },
          ],
          active_run: null,
          pending_events: [],
        }),
      );
      await selectSession2;
    });

    expect(result.current.activeSession.id).toBe("session-3");
    expect(result.current.messages).toEqual([]);
    expect(result.current.messageSessionId).toBe("session-3");

    await act(async () => {
      resolveSession3(
        createJsonResponse({
          session: { id: "session-3", title: "Third" },
          messages: [
            {
              role: "assistant",
              content: [{ type: "text", text: "third session" }],
            },
          ],
          active_run: null,
          pending_events: [],
        }),
      );
      await selectSession3;
    });

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.messageSessionId).toBe("session-3");
      expect(result.current.messages).toHaveLength(1);
    });
    expect(expectChat(result.current.messages[0]).content[0]).toEqual({
      type: "text",
      text: "third session",
      renderKey: "assistant:0:0",
    });
  });

  it("deletes the latest active session and loads the previous session in history", async () => {
    saveActiveSession("/workspace/a", "session-2");

    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [
          { id: "session-2", title: "Second" },
          { id: "session-1", title: "First" },
        ],
      }),
      "/api/sessions/session-1": createJsonResponse({
        session: { id: "session-1", title: "First" },
        messages: [
          {
            role: "assistant",
            content: [{ type: "text", text: "first session" }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
      "/api/sessions/session-2": (init?: RequestInit) =>
        init?.method === "DELETE"
          ? new Response(null, { status: 200 })
          : createJsonResponse({
              session: { id: "session-2", title: "Second" },
              messages: [
                {
                  role: "assistant",
                  content: [{ type: "text", text: "second session" }],
                },
              ],
              active_run: null,
              pending_events: [],
            }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-2");
    });

    await result.current.deleteSession("session-2");

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-1");
      expect(result.current.sessions.map((session) => session.id)).toEqual([
        "session-1",
      ]);
      expect(expectChat(result.current.messages[0]).content[0]).toEqual({
        type: "text",
        text: "first session",
        renderKey: "assistant:0:0",
      });
    });

    expect(loadActiveSession("/workspace/a")).toBe("session-1");
  });

  it("deletes the oldest active session and loads the nearest newer session", async () => {
    saveActiveSession("/workspace/a", "session-1");

    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [
          { id: "session-3", title: "Third" },
          { id: "session-2", title: "Second" },
          { id: "session-1", title: "First" },
        ],
      }),
      "/api/sessions/session-1": (init?: RequestInit) =>
        init?.method === "DELETE"
          ? new Response(null, { status: 200 })
          : createJsonResponse({
              session: { id: "session-1", title: "First" },
              messages: [
                {
                  role: "assistant",
                  content: [{ type: "text", text: "first session" }],
                },
              ],
              active_run: null,
              pending_events: [],
            }),
      "/api/sessions/session-2": createJsonResponse({
        session: { id: "session-2", title: "Second" },
        messages: [
          {
            role: "assistant",
            content: [{ type: "text", text: "second session" }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-1");
    });

    await result.current.deleteSession("session-1");

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-2");
      expect(result.current.sessions.map((session) => session.id)).toEqual([
        "session-3",
        "session-2",
      ]);
      expect(expectChat(result.current.messages[0]).content[0]).toEqual({
        type: "text",
        text: "second session",
        renderKey: "assistant:0:0",
      });
    });

    expect(loadActiveSession("/workspace/a")).toBe("session-2");
  });

  it("replays pending permission requests and clears them on decide", async () => {
    globalThis.localStorage.setItem(
      "mycode.activeSessions",
      JSON.stringify({ "/workspace/a": "session-3" }),
    );
    const decideCalls: string[] = [];
    const encoder = new TextEncoder();
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    const liveStream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-3", title: "Awaiting" }],
      }),
      "/api/sessions/session-3": createJsonResponse({
        session: { id: "session-3", title: "Awaiting" },
        messages: [],
        active_run: {
          id: "run-3",
          session_id: "session-3",
          kind: "chat",
          status: "running",
          last_seq: 0,
        },
        pending_events: [],
      }),
      "/api/runs/run-3/stream?after=0": new Response(liveStream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
      "/api/runs/run-3/decide": (init?: RequestInit) => {
        decideCalls.push(String(init?.body ?? ""));
        streamController.enqueue(
          encoder.encode(
            'data: {"type":"permission_resolved","seq":2,"request_id":"req-1","decision":"allow"}\n\n',
          ),
        );
        return new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.activeSession.id).toBe("session-3");
    });

    streamController.enqueue(
      encoder.encode(
        'data: {"type":"permission_request","seq":1,"request_id":"req-1","tool_use_id":"call-1","tool_name":"bash","preview":"pnpm install"}\n\n',
      ),
    );

    await waitFor(() => {
      expect(result.current.pendingPermission?.request_id).toBe("req-1");
    });
    expect(result.current.pendingPermission?.preview).toBe("pnpm install");

    await result.current.decidePermission("allow");

    await waitFor(() => {
      expect(result.current.pendingPermission).toBeNull();
    });
    expect(decideCalls).toEqual([
      JSON.stringify({ request_id: "req-1", decision: "allow" }),
    ]);

    streamController.close();
  });

  it("keeps the permission prompt visible when decide POST fails", async () => {
    globalThis.localStorage.setItem(
      "mycode.activeSessions",
      JSON.stringify({ "/workspace/a": "session-4" }),
    );
    const encoder = new TextEncoder();
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    const liveStream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-4", title: "Awaiting" }],
      }),
      "/api/sessions/session-4": createJsonResponse({
        session: { id: "session-4", title: "Awaiting" },
        messages: [],
        active_run: {
          id: "run-4",
          session_id: "session-4",
          kind: "chat",
          status: "running",
          last_seq: 0,
        },
        pending_events: [],
      }),
      "/api/runs/run-4/stream?after=0": new Response(liveStream, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
      "/api/runs/run-4/decide": new Response("boom", { status: 500 }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.activeSession.id).toBe("session-4");
    });

    streamController.enqueue(
      encoder.encode(
        'data: {"type":"permission_request","seq":1,"request_id":"req-1","tool_use_id":"call-1","tool_name":"bash","preview":"pnpm install"}\n\n',
      ),
    );

    await waitFor(() => {
      expect(result.current.pendingPermission?.request_id).toBe("req-1");
    });

    await result.current.decidePermission("allow");

    expect(result.current.pendingPermission?.request_id).toBe("req-1");

    streamController.close();
  });

  it("deletes the last active session and falls back to a draft", async () => {
    saveActiveSession("/workspace/a", "session-1");

    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-1", title: "Only Session" }],
      }),
      "/api/sessions/session-1": (init?: RequestInit) =>
        init?.method === "DELETE"
          ? new Response(null, { status: 200 })
          : createJsonResponse({
              session: { id: "session-1", title: "Only Session" },
              messages: [
                {
                  role: "assistant",
                  content: [{ type: "text", text: "only session" }],
                },
              ],
              active_run: null,
              pending_events: [],
            }),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.id).toBe("session-1");
    });

    await result.current.deleteSession("session-1");

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.activeSession.isDraft).toBe(true);
      expect(result.current.sessions).toHaveLength(1);
      expect(result.current.sessions[0]?.id).toBe(
        result.current.activeSession.id,
      );
      expect(result.current.messages).toEqual([]);
    });

    expect(loadActiveSession("/workspace/a")).toBe("");
  });

  const COMPACT_SESSION = {
    session: { id: "session-c", title: "Long chat" },
    messages: [
      { role: "user", content: [{ type: "text", text: "hello" }] },
      {
        role: "assistant",
        content: [{ type: "text", text: "hi there" }],
        meta: {
          context_window: 100_000,
          usage: { total_tokens: 80_000 },
        },
      },
    ],
    active_run: null,
    pending_events: [],
  };

  function mockCompactSessionRoutes(
    routes: Record<string, MockResponse>,
    sessionResponse: MockResponse = createJsonResponse(COMPACT_SESSION),
  ) {
    saveActiveSession("/workspace/a", "session-c");
    return mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-c", title: "Long chat" }],
      }),
      // Longest prefix first: the /compact POST must not hit the session GET.
      ...routes,
      "/api/sessions/session-c": sessionResponse,
    });
  }

  it("reloads the persisted marker and session cost after compaction", async () => {
    let sessionState = { ...COMPACT_SESSION, session_cost: 0.4 };
    const fetchMock = mockCompactSessionRoutes(
      {
        "/api/sessions/session-c/compact": createJsonResponse({
          run: {
            id: "run-c",
            session_id: "session-c",
            kind: "compact",
            status: "running",
            last_seq: 0,
          },
        }),
        "/api/runs/run-c/stream?after=0": () => {
          sessionState = {
            ...sessionState,
            messages: [
              ...sessionState.messages,
              { role: "compact", content: [] },
            ],
            session_cost: 0.41,
          };
          return new Response(
            'data: {"seq":1,"type":"compact"}\n\ndata: [DONE]\n\n',
            { status: 200, headers: { "Content-Type": "text/event-stream" } },
          );
        },
      },
      () => createJsonResponse(sessionState),
    );

    const { result } = renderChatHook({ provider: "anthropic", model: "m1" });
    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.messages).toHaveLength(2);
      expect(result.current.sessionCost).toBe(0.4);
      expect(result.current.currentContext).toEqual({
        tokens: 80_000,
        window: 100_000,
      });
    });

    let compactPromise!: Promise<boolean>;
    act(() => {
      compactPromise = result.current.compactSession();
    });

    // Busy as a compact run, with no optimistic user/assistant turn.
    expect(result.current.runKind).toBe("compact");
    expect(result.current.loading).toBe(true);
    expect(result.current.messages).toHaveLength(2);

    await act(async () => {
      await compactPromise;
    });
    await waitFor(() => {
      expect(result.current.loading).toBe(false);
      expect(result.current.messages).toHaveLength(3);
    });

    const marker = result.current.messages[2];
    expect(marker && isCompactMarker(marker)).toBe(true);
    expect(result.current.sessionCost).toBe(0.41);
    expect(result.current.currentContext).toBeNull();
    expect(result.current.compactError).toBeNull();

    const compactCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/compact"),
    );
    expect(JSON.parse(String(compactCall?.[1]?.body))).toEqual({
      provider: "anthropic",
      model: "m1",
    });
  });

  it("routes compact run errors to compactError and leaves history unchanged", async () => {
    mockCompactSessionRoutes({
      "/api/sessions/session-c/compact": createJsonResponse({
        run: {
          id: "run-c",
          session_id: "session-c",
          kind: "compact",
          status: "running",
          last_seq: 0,
        },
      }),
      "/api/runs/run-c/stream?after=0": new Response(
        'data: {"seq":1,"type":"error","message":"compaction produced no response"}\n\ndata: [DONE]\n\n',
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ),
    });

    const { result } = renderChatHook();
    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    await act(async () => {
      await result.current.compactSession();
    });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.compactError).toBe("compaction produced no response");
    expect(result.current.messages).toHaveLength(2);
    expect(expectChat(result.current.messages[1]).content[0]).toMatchObject({
      type: "text",
      text: "hi there",
    });
  });

  it("surfaces nothing-to-compact from the start request without a run", async () => {
    mockCompactSessionRoutes({
      "/api/sessions/session-c/compact": new Response(
        JSON.stringify({ detail: "nothing to compact" }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      ),
    });

    const { result } = renderChatHook();
    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    let accepted!: boolean;
    await act(async () => {
      accepted = await result.current.compactSession();
    });

    expect(accepted).toBe(false);
    expect(result.current.runKind).toBeNull();
    expect(result.current.compactError).toBe("nothing to compact");
    expect(result.current.messages).toHaveLength(2);
  });

  it("restores compacting state after refresh without optimistic messages", async () => {
    saveActiveSession("/workspace/a", "session-c");
    mockFetch({
      "/api/sessions?cwd=": createJsonResponse({
        sessions: [{ id: "session-c", title: "Long chat" }],
      }),
      "/api/sessions/session-c": createJsonResponse({
        ...COMPACT_SESSION,
        active_run: {
          id: "run-c",
          session_id: "session-c",
          kind: "compact",
          status: "running",
          last_seq: 0,
        },
      }),
      // Keep the run open so the restored state stays observable.
      "/api/runs/run-c/stream?after=0": () => new Promise<Response>(() => {}),
    });

    const { result } = renderChatHook();

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false);
      expect(result.current.runKind).toBe("compact");
    });
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.compactError).toBeNull();
  });

  it("attaches to the existing run kind on a 409 compact start", async () => {
    mockCompactSessionRoutes({
      "/api/sessions/session-c/compact": new Response(
        JSON.stringify({
          detail: {
            message: "session already has a running task",
            run: {
              id: "run-x",
              session_id: "session-c",
              kind: "chat",
              status: "running",
              last_seq: 0,
            },
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
      "/api/runs/run-x/stream?after=0": () => new Promise<Response>(() => {}),
    });

    const { result } = renderChatHook();
    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    await act(async () => {
      await result.current.compactSession();
    });

    await waitFor(() => expect(result.current.runKind).toBe("chat"));
  });

  it("cancel during compact reloads the session state", async () => {
    let cancelled = false;
    mockCompactSessionRoutes({
      "/api/sessions/session-c/compact": createJsonResponse({
        run: {
          id: "run-c",
          session_id: "session-c",
          kind: "compact",
          status: "running",
          last_seq: 0,
        },
      }),
      "/api/runs/run-c/cancel": () => {
        cancelled = true;
        return createJsonResponse({
          status: "ok",
          run: {
            id: "run-c",
            session_id: "session-c",
            kind: "compact",
            status: "cancelled",
            last_seq: 0,
          },
        });
      },
      "/api/runs/run-c/stream?after=0": () => new Promise<Response>(() => {}),
    });

    const { result } = renderChatHook();
    await waitFor(() => expect(result.current.messages).toHaveLength(2));

    act(() => {
      void result.current.compactSession();
    });
    await waitFor(() => expect(result.current.runKind).toBe("compact"));

    await act(async () => {
      result.current.cancel();
    });

    await waitFor(() => {
      expect(cancelled).toBe(true);
      expect(result.current.runKind).toBeNull();
    });
    expect(result.current.messages).toHaveLength(2);
  });
});
