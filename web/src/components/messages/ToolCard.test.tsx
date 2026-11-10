import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type SessionLinks, SessionLinksContext } from "@/components/messages/sessionLinks";
import { ToolCard } from "@/components/messages/ToolCard";

describe("ToolCard web tools", () => {
  it("shows the search query, result count, and plain result body", async () => {
    const user = userEvent.setup();
    render(
      <ToolCard
        id="call-1"
        name="websearch"
        args={{ query: "python typing" }}
        finalOutput={"1. Python docs\nhttps://docs.python.org"}
        metadata={{ results: 1 }}
      />,
    );

    expect(screen.getByText("python typing")).toBeInTheDocument();
    expect(screen.getByText("1 results")).toBeInTheDocument();

    await user.click(screen.getByRole("button"));

    expect(screen.getByText(/1\. Python docs/)).toBeInTheDocument();
  });
});

describe("ToolCard bash", () => {
  it("shows a background command's job status in the collapsed row", () => {
    const card = (metadata?: Record<string, unknown>) => (
      <ToolCard
        id="call-1"
        name="bash"
        args={{ command: "pytest" }}
        finalOutput="Started in background (pid 1): pytest"
        metadata={metadata}
      />
    );
    const { rerender } = render(card({ background: true }));
    expect(screen.getByText("background")).toBeInTheDocument();

    rerender(card({ background: true, running: true }));
    expect(screen.getByText("background · running")).toBeInTheDocument();

    rerender(card({ background: true, exit_code: 2 }));
    expect(screen.getByText("background · exit 2")).toBeInTheDocument();

    rerender(card());
    expect(screen.queryByText("background")).not.toBeInTheDocument();
  });
});

function withLinks(ui: ReactNode, links: SessionLinks) {
  return <SessionLinksContext value={links}>{ui}</SessionLinksContext>;
}

function fakeLinks() {
  return { openAgent: vi.fn(), logUrl: (id: string) => `/api/sessions/s1/logs/${id}` };
}

describe("ToolCard background bash", () => {
  const STARTED =
    "Started in background (pid 1): make\nLog: /tmp/mycode/tool-output/bash-call-1.log";
  const bash = (metadata: Record<string, unknown>) => (
    <ToolCard
      id="call-1"
      name="bash"
      args={{ command: "make all", background: true }}
      finalOutput={STARTED}
      metadata={metadata}
    />
  );

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens to the command and the job's output once it has finished, not the start notice", async () => {
    const user = userEvent.setup();
    render(
      withLinks(
        bash({ background: true, pid: 1, exit_code: 2, job_output: "make: *** [all] Error 2" }),
        fakeLinks(),
      ),
    );

    await user.click(screen.getByRole("button", { expanded: false }));

    expect(screen.getByText("make: *** [all] Error 2")).toBeInTheDocument();
    expect(screen.getAllByText("make all")).toHaveLength(2);
    expect(screen.queryByText(/Started in background/)).toBeNull();
  });

  it("streams a running command's log only while open, resuming after the bytes read", async () => {
    const user = userEvent.setup();
    const encoder = new TextEncoder();
    // One "é" split across the two connections still decodes.
    const bytes = encoder.encode("collected 3 items\né");
    let pulls = 0;
    const first = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (pulls++ === 0) controller.enqueue(bytes.slice(0, -1));
        else controller.error(new TypeError("network"));
      },
    });
    const url = "/api/sessions/s1/logs/call-1";
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const requested = String(input);
      if (requested === `${url}?after=0`) return new Response(first);
      if (requested === `${url}?after=${bytes.length - 1}`) {
        return new Response(new Uint8Array([...bytes.slice(-1), ...encoder.encode(" done\n")]));
      }
      throw new Error(`Unexpected fetch: ${requested}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(withLinks(bash({ background: true, pid: 1, running: true }), fakeLinks()));

    expect(fetchMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { expanded: false }));

    await waitFor(() => expect(screen.getByText("collected 3 items é done")).toBeInTheDocument(), {
      timeout: 3000,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/Started in background/)).toBeNull();
  });
});

describe("ToolCard agent", () => {
  const agent = (props: Partial<Parameters<typeof ToolCard>[0]>) => (
    <ToolCard
      id="call-a"
      name="agent"
      args={{ description: "Map the config", prompt: "Find where config loads" }}
      {...props}
    />
  );

  it("shows the latest three progress lines while a blocking call runs", () => {
    render(agent({ pending: true, output: "Read a.py\nRead b.py\nBash rg config\nEdit c.py\n" }));

    expect(screen.getByText("Map the config")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.queryByText("Read a.py")).toBeNull();
    for (const line of ["Read b.py", "Bash rg config", "Edit c.py"]) {
      expect(screen.getByText(line)).toBeInTheDocument();
    }
  });

  it("renders the final reply as markdown without the notice, with the call's stats", () => {
    render(
      agent({
        finalOutput:
          'Config loads from **two** files.\n\n[Agent call-a finished: 12 tool calls, 45s. Continue it with resume="call-a".]',
        metadata: {
          agent: "general",
          session_id: "call-a",
          background: false,
          status: "completed",
          tool_calls: 12,
          duration_ms: 45210,
          cost: { total: 0.03 },
        },
      }),
    );

    expect(screen.getByText("12 tools · 45s · $0.03")).toBeInTheDocument();
    expect(screen.getByText("two").tagName).toBe("STRONG");
    expect(screen.queryByText(/Continue it with resume/)).toBeNull();
  });

  it("marks a failed call and keeps its error text", () => {
    render(
      agent({
        finalOutput: "partial\nerror: max_turns reached",
        isError: true,
        metadata: {
          agent: "general",
          session_id: "call-a",
          background: false,
          status: "failed",
          tool_calls: 3,
          duration_ms: 1000,
        },
      }),
    );

    expect(screen.getByText("failed · 3 tools · 1s")).toBeInTheDocument();
    expect(screen.getByText(/error: max_turns reached/)).toBeInTheDocument();
  });

  it("opens a background start only once its job has replied, and follows the job in the pill", async () => {
    const user = userEvent.setup();
    const start = { agent: "general", session_id: "call-a", background: true };
    const card = (metadata: Record<string, unknown>) =>
      agent({ finalOutput: "Started agent in background: Map the config", metadata });
    const { rerender } = render(card(start));
    expect(screen.getByText("background")).toBeInTheDocument();

    rerender(card({ ...start, running: true }));
    expect(screen.getByText("background · running")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Map the config/ })).toBeNull();

    rerender(
      card({
        ...start,
        status: "cancelled",
        tool_calls: 2,
        duration_ms: 4000,
        cost: { total: 0.01 },
        job_output: "Config loads from **two** files.",
      }),
    );
    expect(screen.getByText("stopped · 2 tools · 4s · $0.01")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Map the config/ }));
    expect(screen.getByText("two").tagName).toBe("STRONG");
    expect(screen.queryByText(/Started agent in background/)).toBeNull();
  });

  it("toggles its body on a row click and opens the child its call names", async () => {
    const user = userEvent.setup();
    const links = fakeLinks();
    // No transcript to open until the call names its child.
    const { rerender } = render(withLinks(agent({ pending: true, output: "" }), links));
    expect(screen.queryByRole("button", { name: "Open transcript" })).toBeNull();

    const named = { agent: "general", session_id: "child-a", background: false };
    rerender(withLinks(agent({ pending: true, output: "Read a.py\n", metadata: named }), links));
    const row = screen.getByRole("button", { name: /Map the config/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    await user.click(row);
    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(links.openAgent).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Open transcript" }));
    expect(links.openAgent).toHaveBeenLastCalledWith("child-a");

    // A resume names the child it continues.
    rerender(
      withLinks(
        agent({
          id: "call-b",
          args: { description: "Go on", prompt: "…", resume: "child-a" },
          metadata: named,
        }),
        links,
      ),
    );
    await user.click(screen.getByRole("button", { name: "Open transcript" }));
    expect(links.openAgent).toHaveBeenLastCalledWith("child-a");
  });
});
