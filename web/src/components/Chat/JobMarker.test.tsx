import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { JobMarker } from "./JobMarker";

describe("JobMarker", () => {
  it("names the event, the command and its exit code, and opens to the output", async () => {
    const user = userEvent.setup();
    render(
      <JobMarker
        jobs={[
          {
            tool_use_id: "call-1",
            label: "pytest -q",
            exit_code: 1,
            output: "FAILED test_a\n\n1 failed",
          },
        ]}
      />,
    );

    const row = screen.getByRole("button", { expanded: false });
    expect(row).toHaveTextContent(/^Background finished/);
    expect(within(row).getByText("pytest -q")).toBeInTheDocument();
    expect(within(row).getByText("exit 1")).toBeInTheDocument();

    await user.click(row);

    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/FAILED test_a/)).toBeInTheDocument();
  });

  it("renders one row per finished command", () => {
    render(
      <JobMarker
        jobs={[
          { tool_use_id: "a", label: "make", exit_code: 0, output: "ok" },
          { tool_use_id: "b", label: "lint", exit_code: 0, output: "" },
        ]}
      />,
    );

    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.getAllByText("exit 0")).toHaveLength(2);
  });
});
