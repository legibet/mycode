import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerEvent } from "@/api";
import { TranscriptDialog } from "@/components/transcript/TranscriptDialog";
import type { SubscribeServerEvents } from "@/hooks/useChat";
import type { ChatMessage, RenderMessage, TranscriptTarget } from "@/types";
import { buildRenderMessages } from "@/utils/messages";

vi.mock("@/hooks/useIsDesktop", () => ({
  useIsDesktop: () => true,
}));

// The app's `/api/events` subscription, driven by the tests.
const listeners = new Set<Parameters<SubscribeServerEvents>[0]>();
const subscribeEvents: SubscribeServerEvents = (listener) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};
const events = {
  emit(event: ServerEvent) {
    act(() => {
      for (const listener of listeners) listener.onEvent(event);
    });
  },
  reconnect() {
    act(() => {
      for (const listener of listeners) listener.onReconnect();
    });
  },
};

const CHILD_URL = "/api/sessions/p1/agents/c1";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A stream response that stays open until `end()` sends `[DONE]`. */
function openStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const end = () => {
    controller.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
    controller.close();
  };
  return { response: new Response(stream, { status: 200 }), end };
}

type Route = Response | (() => Response);

function mockFetch(routes: Record<string, Route>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const route = routes[url];
    if (!route) throw new Error(`Unexpected fetch: ${url}`);
    return typeof route === "function" ? route() : route.clone();
  });
  globalThis.fetch = fetchMock as typeof fetch;
  return fetchMock;
}

const run = {
  id: "run-c",
  session_id: "p1/c1",
  kind: "agent",
  status: "running",
  last_seq: 0,
} as const;

function childSnapshot(data: Record<string, unknown>) {
  return json({
    session: {
      id: "c1",
      agent: "reviewer",
      description: "Map the config",
      provider: "anthropic",
      model: "claude-sonnet-5-5",
      created_at: "2026-11-06T10:00:00+00:00",
    },
    messages: [
      {
        role: "user",
        content: [{ type: "text", text: "Find where config loads" }],
        meta: { created_at: new Date(Date.now() - 65_000).toISOString() },
      },
    ],
    session_usage: null,
    session_cost: null,
    active_run: null,
    pending_events: [],
    pending: { steers: [], queue: [] },
    jobs: [],
    ...data,
  });
}

/** The parent's turn that started the child, with the call `call-s`. */
const spawnTurn: ChatMessage[] = [
  { role: "user", content: [{ type: "text", text: "look around" }] },
  {
    role: "assistant",
    content: [
      {
        type: "tool_use",
        id: "call-s",
        name: "agent",
        input: { description: "Map the config", prompt: "…", agent: "reviewer" },
      },
    ],
  },
];
/** A running `agent` call that has named its child. */
const runningCall = (childId: string) => ({
  pending: true,
  output: "",
  finalOutput: null,
  metadata: { agent: "reviewer", session_id: childId, background: false },
  isError: false,
});
/** The starting call still running. */
const parentMessages: RenderMessage[] = buildRenderMessages(spawnTurn, {
  "call-s": runningCall("c1"),
});

type DialogProps = ComponentProps<typeof TranscriptDialog>;

function dialog(transcript: TranscriptTarget | null, props: Partial<DialogProps> = {}) {
  return (
    <TranscriptDialog
      transcript={transcript}
      messages={parentMessages}
      jobs={[]}
      onOpen={vi.fn()}
      onClose={vi.fn()}
      onStopJob={vi.fn()}
      subscribeEvents={subscribeEvents}
      permission={null}
      onDecidePermission={vi.fn()}
      {...props}
    />
  );
}

function renderDialog(transcript: TranscriptTarget, props: Partial<DialogProps> = {}) {
  const handlers = {
    onOpen: vi.fn(),
    onClose: vi.fn(),
    onStopJob: vi.fn(),
    onDecidePermission: vi.fn(),
  };
  const result = render(dialog(transcript, { ...handlers, ...props }));
  return { ...handlers, ...result };
}

const childTranscript: TranscriptTarget = { sessionId: "p1", childId: "c1" };

describe("TranscriptDialog", () => {
  beforeEach(() => {
    listeners.clear();
  });

  it("shows a child whose run has not started as starting, and follows the run once it starts", async () => {
    const snapshots = vi
      .fn()
      .mockReturnValueOnce(childSnapshot({ messages: [] }))
      .mockReturnValue(childSnapshot({ active_run: run }));
    const stream = openStream();
    mockFetch({
      [CHILD_URL]: () => snapshots(),
      "/api/runs/run-c/stream?after=0": stream.response,
    });
    renderDialog(childTranscript);

    expect(await screen.findByText("starting")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Map the config" })).toBeInTheDocument();
    expect(screen.getByText("Waiting for the agent to start…")).toBeInTheDocument();

    events.emit({ type: "run_started", session_id: "p1/other", run });
    expect(snapshots).toHaveBeenCalledTimes(1);
    events.emit({ type: "run_started", session_id: "p1/c1", run });

    expect(await screen.findByText("Find where config loads")).toBeInTheDocument();
    expect(screen.getByText(/^running/)).toBeInTheDocument();
    // The run it already follows.
    events.emit({ type: "run_started", session_id: "p1/c1", run });
    expect(snapshots).toHaveBeenCalledTimes(2);
    stream.end();
  });

  it("shows why a child cannot load", async () => {
    mockFetch({ [CHILD_URL]: json({ detail: "agent not found" }, 404) });
    renderDialog(childTranscript);

    expect(await screen.findByText("agent not found")).toBeInTheDocument();
    // The call that started it still names it.
    expect(screen.getByRole("heading", { name: "Map the config" })).toBeInTheDocument();
  });

  it("shows a running child's elapsed time and stops its background job", async () => {
    const user = userEvent.setup();
    const stream = openStream();
    mockFetch({
      [CHILD_URL]: childSnapshot({ active_run: run }),
      "/api/runs/run-c/stream?after=0": stream.response,
    });
    const { onStopJob } = renderDialog(childTranscript, {
      jobs: [
        {
          id: "job-r",
          tool_use_id: "call-r",
          name: "agent",
          label: "Map the config",
          session_id: "c1",
          started_at: new Date().toISOString(),
        },
      ],
    });

    expect(await screen.findByText("running · 1m 5s")).toBeInTheDocument();
    expect(screen.getByText("reviewer")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStopJob).toHaveBeenCalledWith("job-r");
    stream.end();
  });

  it("times the turn from its opening message, not from a message steered into it", async () => {
    const stream = openStream();
    mockFetch({
      [CHILD_URL]: childSnapshot({
        active_run: run,
        messages: [
          {
            role: "user",
            content: [{ type: "text", text: "Find where config loads" }],
            meta: { created_at: new Date(Date.now() - 65_000).toISOString() },
          },
          { role: "assistant", content: [{ type: "text", text: "Looking." }] },
          {
            role: "user",
            content: [{ type: "text", text: "also check the env" }],
            meta: { created_at: new Date(Date.now() - 5_000).toISOString(), steer: true },
          },
        ],
      }),
      "/api/runs/run-c/stream?after=0": stream.response,
    });
    renderDialog(childTranscript);

    expect(await screen.findByText("running · 1m 5s")).toBeInTheDocument();
    stream.end();
  });

  it("reloads when the run ends and shows the finished turn's stats", async () => {
    const last = {
      status: "completed",
      tool_calls: 12,
      duration_ms: 45_000,
      cost: { total: 0.03 },
      finished_at: "2026-11-06T10:01:00+00:00",
    };
    const snapshots = vi
      .fn()
      .mockReturnValueOnce(childSnapshot({ active_run: run }))
      .mockReturnValue(
        childSnapshot({
          session: {
            id: "c1",
            agent: "reviewer",
            description: "Map the config",
            provider: "anthropic",
            model: "claude-sonnet-5-5",
            created_at: "2026-11-06T10:00:00+00:00",
            last,
          },
        }),
      );
    const stream = openStream();
    mockFetch({
      [CHILD_URL]: () => snapshots(),
      "/api/runs/run-c/stream?after=0": stream.response,
    });
    renderDialog(childTranscript);
    expect(await screen.findByText(/^running/)).toBeInTheDocument();

    stream.end();

    expect(await screen.findByText("12 tools · 45s · $0.03")).toBeInTheDocument();
    expect(snapshots).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  it("shows a child with no run and no finished turn as interrupted", async () => {
    mockFetch({ [CHILD_URL]: childSnapshot({}) });
    renderDialog(childTranscript, { messages: buildRenderMessages(spawnTurn) });

    expect(await screen.findByText("interrupted")).toBeInTheDocument();
    // The history renders in a transition, after the header.
    expect(await screen.findByText("Find where config loads")).toBeInTheDocument();
  });

  it("decides starting from the child's own call, not from other work in the parent", async () => {
    mockFetch({ [CHILD_URL]: childSnapshot({}) });
    const otherCall: ChatMessage = {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "call-o",
          name: "agent",
          input: { description: "Other", prompt: "…" },
        },
      ],
    };
    const resumeCall: ChatMessage = {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "call-r",
          name: "agent",
          input: { description: "Go on", prompt: "…", resume: "c1" },
        },
      ],
    };
    const withMessages = (messages: RenderMessage[]) => dialog(childTranscript, { messages });
    const { rerender } = render(
      withMessages(buildRenderMessages([...spawnTurn, otherCall], { "call-o": runningCall("c2") })),
    );
    expect(await screen.findByText("interrupted")).toBeInTheDocument();

    rerender(
      withMessages(
        buildRenderMessages([...spawnTurn, resumeCall], { "call-r": runningCall("c1") }),
      ),
    );
    expect(screen.getByText("starting")).toBeInTheDocument();
  });

  it("loads when it opens, and again when its child's run starts or the events reconnect", async () => {
    const fetchMock = mockFetch({ [CHILD_URL]: () => childSnapshot({}) });
    renderDialog(childTranscript, { messages: buildRenderMessages(spawnTurn) });
    expect(await screen.findByText("interrupted")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    events.emit({ type: "run_finished", session_id: "p1/c1", run });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    events.emit({ type: "run_started", session_id: "p1/c1", run });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    events.reconnect();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("drops a snapshot that answers after the dialog closed, without following its run", async () => {
    let answer!: (response: Response) => void;
    let signal: AbortSignal | null | undefined;
    const fetchMock = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      signal = init?.signal;
      return new Promise<Response>((resolve) => {
        answer = resolve;
      });
    });
    globalThis.fetch = fetchMock as typeof fetch;
    const { unmount } = render(dialog(childTranscript));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => answer(childSnapshot({ active_run: run })));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("closes from its header", async () => {
    const user = userEvent.setup();
    mockFetch({ [CHILD_URL]: childSnapshot({}) });
    const { onClose } = renderDialog(childTranscript);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("answers the parent's permission request inside the dialog, without closing it", async () => {
    const user = userEvent.setup();
    mockFetch({ [CHILD_URL]: childSnapshot({}) });
    const { onDecidePermission, onClose } = renderDialog(childTranscript, {
      permission: {
        request_id: "req-1",
        tool_use_id: "call-b",
        tool_name: "bash",
        preview: "ls",
        agent: "Map the config",
      },
    });

    const prompt = within(screen.getByRole("dialog", { name: "Agent transcript" })).getByRole(
      "dialog",
      {
        name: "Tool permission request",
      },
    );
    expect(within(prompt).getByText("agent: Map the config")).toBeInTheDocument();
    await user.click(within(prompt).getByRole("button", { name: /allow/i }));
    fireEvent.keyDown(prompt, { key: "Escape" });

    expect(onDecidePermission.mock.calls).toEqual([["allow"], ["deny"]]);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("swaps the child in place when another one opens", async () => {
    mockFetch({
      [CHILD_URL]: childSnapshot({}),
      "/api/sessions/p1/agents/c2": childSnapshot({
        session: {
          id: "c2",
          agent: "reviewer",
          description: "Read the docs",
          provider: "anthropic",
          model: "claude-sonnet-5-5",
          created_at: "2026-11-06T10:00:00+00:00",
        },
      }),
    });
    const { rerender } = renderDialog(childTranscript);
    const shell = screen.getByRole("dialog", { name: "Agent transcript" });
    expect(await screen.findByRole("heading", { name: "Map the config" })).toBeInTheDocument();

    rerender(dialog({ sessionId: "p1", childId: "c2" }));

    expect(await screen.findByRole("heading", { name: "Read the docs" })).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Agent transcript" })).toBe(shell);
  });
});
