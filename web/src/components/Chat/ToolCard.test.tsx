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
