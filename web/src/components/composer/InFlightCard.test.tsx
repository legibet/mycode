import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackgroundJobInfo } from "@/api";
import { InFlightCard } from "@/components/composer/InFlightCard";
import { SessionLinksContext } from "@/components/messages/sessionLinks";
import type { ChatMessage } from "@/types";
import { buildRenderMessages } from "@/utils/messages";

const LONG_COMMAND = `for i in $(seq 1 300); do echo "tick $i of the long running pipeline"; sleep 1; done && echo "all modules built, artifacts written to ./dist/output/bundle"`;

const started = new Date().toISOString();
const bashJob: BackgroundJobInfo = {
  tool_use_id: "call-1",
  name: "bash",
  label: `${LONG_COMMAND.slice(0, 77)}...`,
  pid: 7,
  started_at: started,
};
const lintJob: BackgroundJobInfo = {
  tool_use_id: "call-2",
  name: "bash",
  label: "pnpm lint",
  pid: 8,
  started_at: started,
};
const agentJob: BackgroundJobInfo = {
  tool_use_id: "call-r",
  name: "agent",
  label: "Map the config",
  session_id: "call-a",
  started_at: started,
};

/** The turn that started call-1, with the job's notification once it has finished. */
function history(exitCode?: number) {
  const messages: ChatMessage[] = [
    { role: "user", content: [{ type: "text", text: "build it" }] },
    {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "call-1",
          name: "bash",
          input: { command: LONG_COMMAND, background: true },
        },
      ],
    },
    {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "call-1",
          output: "Started in background (pid 7)",
          metadata: { background: true, pid: 7 },
          is_error: false,
        },
      ],
    },
  ];
  if (exitCode !== undefined) {
    messages.push({
      role: "user",
      content: [
        {
          type: "text",
          text: `Background bash finished (pid 7, exit code ${exitCode})\n\ntick 300`,
          meta: {
            job: { tool_use_id: "call-1", name: "bash", label: "for i", exit_code: exitCode },
          },
        },
      ],
    });
  }
  return buildRenderMessages(messages);
}

const links = {
  openAgent: vi.fn(),
  logUrl: (id: string) => `/api/sessions/s1/logs/${id}`,
};

type CardProps = ComponentProps<typeof InFlightCard>;

function card(props: Partial<CardProps>) {
  return (
    <SessionLinksContext value={links}>
      <InFlightCard
        jobs={[]}
        onStopJob={vi.fn()}
        messages={history()}
        queued={[]}
        onSteerQueued={vi.fn()}
        onEditQueued={vi.fn()}
        onRemoveQueued={vi.fn()}
        {...props}
      />
    </SessionLinksContext>
  );
}

describe("InFlightCard background jobs", () => {
  beforeEach(() => {
    links.openAgent.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("tick 1\ntick 2\n")),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sums up the running jobs in one bar that opens a list of them", async () => {
    const user = userEvent.setup();
    render(card({ jobs: [bashJob, lintJob, agentJob] }));

    const bar = screen.getByRole("button", { name: "Background: 2 commands, 1 agent" });
    expect(screen.queryByRole("list", { name: "Background jobs" })).toBeNull();

    await user.click(bar);

    const list = await screen.findByRole("list", { name: "Background jobs" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    // A command's row shows its full command, not the shortened label.
    expect(within(list).getByText(LONG_COMMAND)).toBeInTheDocument();
  });

  it("stops a job from its row", async () => {
    const user = userEvent.setup();
    const onStopJob = vi.fn();
    render(card({ jobs: [lintJob, agentJob], onStopJob }));

    await user.click(screen.getByRole("button", { name: /^Background/ }));
    const list = await screen.findByRole("list", { name: "Background jobs" });
    const agentRow = within(list).getByText("Map the config").closest("li")!;
    await user.click(within(agentRow).getByRole("button", { name: "Stop" }));

    expect(onStopJob).toHaveBeenCalledWith("call-r");
  });

  it("opens an agent's transcript from its row and closes the panel", async () => {
    const user = userEvent.setup();
    render(card({ jobs: [lintJob, agentJob] }));

    await user.click(screen.getByRole("button", { name: /^Background/ }));
    await user.click(await screen.findByRole("button", { name: /Map the config/ }));

    expect(links.openAgent).toHaveBeenCalledWith("call-a");
    expect(screen.queryByRole("list", { name: "Background jobs" })).toBeNull();
  });

  it("opens a lone command straight to its live output; Esc steps back to the list, then closes", async () => {
    const user = userEvent.setup();
    // The window is where App stops the run on Esc.
    const windowKeys = vi.fn();
    window.addEventListener("keydown", windowKeys);
    render(card({ jobs: [bashJob] }));

    await user.click(screen.getByRole("button", { name: /^Background/ }));

    expect(await screen.findByText(LONG_COMMAND)).toBeInTheDocument();
    expect(await screen.findByText(/tick 2/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Background jobs" })).toBeNull();

    await user.keyboard("{Escape}");
    expect(screen.getByRole("list", { name: "Background jobs" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("list", { name: "Background jobs" })).toBeNull();
    expect(windowKeys).not.toHaveBeenCalled();
    window.removeEventListener("keydown", windowKeys);
  });

  it("keeps a finished command on screen until the reader leaves it", async () => {
    const user = userEvent.setup();
    const { rerender } = render(card({ jobs: [bashJob] }));
    await user.click(screen.getByRole("button", { name: /^Background/ }));
    expect(await screen.findByText(/running ·/)).toBeInTheDocument();

    rerender(card({ jobs: [] }));
    expect(screen.getByText("finished")).toBeInTheDocument();

    rerender(card({ jobs: [], messages: history(0) }));
    expect(screen.getByText("exit 0")).toBeInTheDocument();
    expect(screen.getByText(LONG_COMMAND)).toBeInTheDocument();

    // Back to a list with nothing in it: the panel and the bar go.
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.queryByText(LONG_COMMAND)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Background/ })).toBeNull();
  });
});
