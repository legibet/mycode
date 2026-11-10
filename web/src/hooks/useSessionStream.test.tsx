import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RunInfo, SessionResponse } from "@/api";
import { type StreamEnd, useSessionStream } from "@/hooks/useSessionStream";
import type { ChatMessage, RenderMessage } from "@/types";
import { isChatMessage } from "@/types";

const RUN: RunInfo = {
  id: "run-1",
  session_id: "p/c",
  kind: "agent",
  status: "running",
  last_seq: 1,
};

function sse(lines: string[]): Response {
  return new Response(lines.map((line) => `data: ${line}\n\n`).join(""), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** A stream response that stays open until `end()` sends `[DONE]`, and
 * fails like a real fetch body when its request is aborted. */
function openStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const respond = (init?: RequestInit) => {
    init?.signal?.addEventListener("abort", () =>
      controller.error(new DOMException("aborted", "AbortError")),
    );
    return new Response(stream, { status: 200 });
  };
  const end = () => {
    controller.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
    controller.close();
  };
  return { respond, end };
}

function snapshot(data: Partial<SessionResponse<unknown>>): SessionResponse<unknown> {
  return {
    session: null,
    messages: [],
    session_usage: null,
    session_cost: null,
    active_run: null,
    pending_events: [],
    pending: { steers: [], queue: [] },
    jobs: [],
    ...data,
  };
}

function tailText(messages: RenderMessage[]): string {
  const tail = messages.at(-1);
  if (!tail || !isChatMessage(tail)) throw new Error("expected a chat message");
  return tail.content.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("");
}

const prompt: ChatMessage = { role: "user", content: [{ type: "text", text: "find it" }] };

function renderStream(reload = vi.fn(async () => null)) {
  const hook = renderHook(() => useSessionStream({ initialSessionId: "p/c", reload }));
  return { ...hook, reload };
}

describe("useSessionStream", () => {
  it("replays a snapshot's pending events, then streams the rest of its run", async () => {
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      expect(String(url)).toBe("/api/runs/run-1/stream?after=1");
      return sse([JSON.stringify({ type: "text", delta: "lo", seq: 2 }), "[DONE]"]);
    }) as typeof fetch;
    const { result } = renderStream();

    let end: Promise<StreamEnd | null> | undefined;
    act(() => {
      const { run, after, stopped } = result.current.applySnapshot(
        snapshot({
          messages: [prompt],
          active_run: RUN,
          pending_events: [{ type: "text", delta: "hel", seq: 1 }],
        }),
        "p/c",
      );
      expect(after).toBe(1);
      if (run) end = result.current.follow(run, "p/c", after, stopped);
    });

    await act(async () => {
      await expect(end).resolves.toMatchObject({ sawDone: true, stopped: false });
    });
    expect(tailText(result.current.messages)).toBe("hello");
    expect(result.current.isStreaming()).toBe(false);
  });

  it("reloads the session when the stream drops, and fails the turn when it cannot", async () => {
    globalThis.fetch = vi.fn(async () =>
      sse([JSON.stringify({ type: "text", delta: "partial", seq: 2 })]),
    ) as typeof fetch;
    const reload = vi.fn(async () => {
      throw new Error("offline");
    });
    const { result } = renderStream(reload);
    act(() => {
      result.current.applySnapshot(snapshot({ messages: [prompt] }), "p/c");
    });

    let end: StreamEnd | null = null;
    await act(async () => {
      end = await result.current.follow(RUN, "p/c", 1);
    });

    expect(reload).toHaveBeenCalledWith("p/c");
    expect(end).toMatchObject({ sawDone: false, stopped: true });
    const tail = result.current.messages.at(-1);
    expect(tail && isChatMessage(tail) ? tail.meta?.error : null).toBe(
      "Stream disconnected. Reload the session to resume.",
    );
  });

  it("ends a dropped stream whose reload finds the run over", async () => {
    globalThis.fetch = vi.fn(async () =>
      sse([JSON.stringify({ type: "text", delta: "partial", seq: 2 })]),
    ) as typeof fetch;
    const { result, reload } = renderStream();
    act(() => {
      result.current.applySnapshot(snapshot({ messages: [prompt] }), "p/c");
    });

    let end: StreamEnd | null = null;
    await act(async () => {
      end = await result.current.follow(RUN, "p/c", 1);
    });

    expect(reload).toHaveBeenCalledWith("p/c");
    expect(end).toMatchObject({ sawDone: false, stopped: false });
    expect(result.current.isStreaming()).toBe(false);
    expect(result.current.activeRunRef.current).toBeNull();
  });

  it("hands the state to a newer stream and resolves the older one with null", async () => {
    const first = openStream();
    const second = openStream();
    globalThis.fetch = vi
      .fn()
      .mockImplementationOnce(async (_url, init?: RequestInit) => first.respond(init))
      .mockImplementationOnce(async (_url, init?: RequestInit) =>
        second.respond(init),
      ) as typeof fetch;
    const { result } = renderStream();

    let older!: Promise<StreamEnd | null>;
    let newer!: Promise<StreamEnd | null>;
    act(() => {
      older = result.current.follow(RUN, "p/c");
    });
    await waitFor(() => expect(result.current.isStreaming()).toBe(true));
    act(() => {
      newer = result.current.follow({ ...RUN, id: "run-2" }, "p/c");
    });
    expect(result.current.activeRunRef.current?.id).toBe("run-2");

    await expect(older).resolves.toBeNull();
    second.end();
    await expect(newer).resolves.toMatchObject({ sawDone: true });
  });

  it("aborts the followed stream when it unmounts", async () => {
    const { respond } = openStream();
    let signal: AbortSignal | undefined;
    globalThis.fetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      signal = init?.signal ?? undefined;
      return respond(init);
    }) as typeof fetch;
    const { result, unmount } = renderStream();

    let end!: Promise<StreamEnd | null>;
    act(() => {
      end = result.current.follow(RUN, "p/c");
    });
    await waitFor(() => expect(signal).toBeDefined());
    unmount();

    expect(signal?.aborted).toBe(true);
    await expect(end).resolves.toBeNull();
  });
});
