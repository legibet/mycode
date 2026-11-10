import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PermissionPrompt } from "@/components/composer/PermissionPrompt";

const REQUEST = { request_id: "req-1", tool_use_id: "call-1", tool_name: "bash", preview: "ls" };

describe("PermissionPrompt", () => {
  it("allows on Enter in the prompt and denies on Escape", () => {
    const onDecide = vi.fn();
    render(<PermissionPrompt request={REQUEST} onDecide={onDecide} />);
    const dialog = screen.getByRole("dialog");

    fireEvent.keyDown(dialog, { key: "Enter" });
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onDecide.mock.calls).toEqual([["allow"], ["deny"]]);
  });

  it("leaves Enter on a focused button to that button", () => {
    const onDecide = vi.fn();
    render(<PermissionPrompt request={REQUEST} onDecide={onDecide} />);

    fireEvent.keyDown(screen.getByRole("button", { name: /deny/i }), { key: "Enter" });

    expect(onDecide).not.toHaveBeenCalled();
  });

  it("names the subagent whose call asks", () => {
    const { rerender } = render(<PermissionPrompt request={REQUEST} onDecide={vi.fn()} />);
    expect(screen.queryByText(/^agent:/)).toBeNull();

    rerender(
      <PermissionPrompt request={{ ...REQUEST, agent: "Map the config" }} onDecide={vi.fn()} />,
    );
    expect(screen.getByText("agent: Map the config")).toBeInTheDocument();
  });
});
