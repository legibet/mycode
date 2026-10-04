import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ToolCard } from "./ToolCard";

describe("ToolCard web tools", () => {
  it("shows the search query, result count, and plain result body", async () => {
    const user = userEvent.setup();
    render(
      <ToolCard
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
