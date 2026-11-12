import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { JobMarker } from "@/components/messages/JobMarker";
import { SessionLinksContext } from "@/components/messages/sessionLinks";

describe("JobMarker", () => {
  it("names the event, the command and its exit code, and opens to the full command and output", async () => {
    const user = userEvent.setup();
    const command = `pytest -q ${"tests/test_long_module_name.py ".repeat(4)}`.trim();
    render(
      <JobMarker
        jobs={[
          {
            name: "bash",
            id: "job-1",
            tool_use_id: "call-1",
            label: `${command.slice(0, 77)}...`,
            command,
            exit_code: 1,
            output: "FAILED test_a\n\n1 failed",
          },
        ]}
      />,
    );

    const row = screen.getByRole("button", { expanded: false });
    expect(row).toHaveTextContent(/^Background finished/);
    expect(within(row).getByText("exit 1")).toBeInTheDocument();

    await user.click(row);

    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByText(command)).toHaveLength(2);
    expect(screen.getByText(/FAILED test_a/)).toBeInTheDocument();
  });

  it("renders one row per finished command", () => {
    render(
      <JobMarker
        jobs={[
          {
            name: "bash",
            id: "job-a",
            tool_use_id: "a",
            label: "make",
            command: "make",
            exit_code: 0,
            output: "ok",
          },
          {
            name: "bash",
            id: "job-b",
            tool_use_id: "b",
            label: "lint",
            command: "lint",
            exit_code: 0,
            output: "",
          },
        ]}
      />,
    );

    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.getAllByText("exit 0")).toHaveLength(2);
  });

  it("names a finished agent with its stats, opens to its reply, and opens its transcript", async () => {
    const user = userEvent.setup();
    const links = { openAgent: vi.fn() };
    render(
      <SessionLinksContext value={links}>
        <JobMarker
          jobs={[
            {
              name: "agent",
              id: "job-r",
              tool_use_id: "call-r",
              label: "Map the config",
              session_id: "call-a",
              status: "completed",
              tool_calls: 1,
              duration_ms: 8000,
              output: "Config loads from **two** files.",
            },
          ]}
        />
      </SessionLinksContext>,
    );

    const row = screen.getByRole("button", { expanded: false });
    expect(row).toHaveTextContent(/^Agent finished/);
    expect(within(row).getByText("Map the config")).toBeInTheDocument();
    expect(within(row).getByText("1 tool · 8s")).toBeInTheDocument();

    await user.click(row);
    expect(screen.getByText("two").tagName).toBe("STRONG");

    await user.click(screen.getByRole("button", { name: "Open transcript" }));
    expect(links.openAgent).toHaveBeenCalledWith("call-a");
  });

  it.each([
    ["cancelled", "Agent stopped", "stopped · 7 tools · 12s · $0.02"],
    ["failed", "Agent failed", "failed · 7 tools · 12s · $0.02"],
  ] as const)("names an agent that ended %s by how it ended", (status, heading, pill) => {
    render(
      <JobMarker
        jobs={[
          {
            name: "agent",
            id: "job-r",
            tool_use_id: "call-r",
            label: "Slow file survey",
            session_id: "call-a",
            status,
            tool_calls: 7,
            duration_ms: 12_000,
            cost: { total: 0.02 },
            output: "",
          },
        ]}
      />,
    );

    const row = screen.getByRole("button", { expanded: false });
    expect(row).toHaveTextContent(new RegExp(`^${heading}`));
    expect(within(row).getByText(pill)).toBeInTheDocument();
  });
});
