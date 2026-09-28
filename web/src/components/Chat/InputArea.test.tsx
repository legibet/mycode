import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Cost } from "../../types";
import { InputArea } from "./InputArea";

describe("InputArea", () => {
  it("rejects new unsupported media and keeps existing uploads while blocking submission", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const onSubmit = vi.fn().mockResolvedValue(true);
    const onAttachFiles = vi.fn();
    const image = {
      id: "image-1",
      kind: "image" as const,
      data: "base64",
      mime_type: "image/png",
      name: "diagram.png",
      preview: "blob:diagram",
    };

    const { rerender } = render(
      <InputArea
        loading={false}
        onSubmit={onSubmit}
        onCancel={() => {}}
        files={[]}
        onAttachFiles={onAttachFiles}
        supportsImages={false}
        config={{
          provider: "anthropic",
          model: "text-only",
          cwd: "/workspace",
          reasoningEfforts: {},
        }}
        remoteConfig={null}
        onUpdateConfig={() => {}}
      />,
    );

    await user.upload(
      screen.getByLabelText("Attach files"),
      new File(["image"], "new.png", { type: "image/png" }),
    );

    expect(onAttachFiles).not.toHaveBeenCalled();
    expect(screen.getByText("Image unsupported")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "text-only" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "medium" })).toBeNull();

    rerender(
      <InputArea
        loading={false}
        onSubmit={onSubmit}
        onCancel={() => {}}
        files={[image]}
        onAttachFiles={onAttachFiles}
        supportsImages={false}
        config={{
          provider: "anthropic",
          model: "text-only",
          cwd: "/workspace",
          reasoningEfforts: {},
        }}
        remoteConfig={null}
        onUpdateConfig={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(onSubmit).not.toHaveBeenCalled());
    expect(screen.getByAltText("diagram.png")).toBeInTheDocument();
    expect(
      screen.getByText("Remove image or switch model"),
    ).toBeInTheDocument();
    expect(screen.getByText("Image unsupported")).toBeInTheDocument();
  });

  it("shows session usage in a card and warns before auto-compact", async () => {
    const user = userEvent.setup();
    const renderStats = (
      tokens: number,
      cost: Cost = {
        input: 0.08,
        cache_read: 0.05,
        output: 0.26,
        total: 0.39,
      },
    ) =>
      render(
        <InputArea
          loading={false}
          onSubmit={vi.fn()}
          onCancel={() => {}}
          config={{
            provider: "anthropic",
            model: "m",
            cwd: "/workspace",
            reasoningEfforts: {},
          }}
          remoteConfig={{ compact_threshold: 0.8 }}
          onUpdateConfig={() => {}}
          currentContext={{ tokens, window: 200_000 }}
          sessionUsage={{
            total_tokens: 1_040_000,
            input_tokens: 1_000_000,
            cache_read_tokens: 860_000,
            output_tokens: 40_000,
            cost,
          }}
        />,
      );

    const { unmount } = renderStats(54_000);
    expect(screen.getByText("27%")).not.toHaveClass("text-destructive/70");

    await user.click(screen.getByText("27%"));
    const card = await screen.findByRole("dialog");
    expect(card).toHaveTextContent("Context54,000 / 200,000");
    expect(card).toHaveTextContent("Cache hit86%");
    expect(card).toHaveTextContent("Input140,000$0.08");
    expect(card).toHaveTextContent("Cache read860,000$0.05");
    expect(card).toHaveTextContent("Total1,040,000$0.39");
    unmount();

    renderStats(144_000, { total: 0.39 });
    expect(screen.getByText("72%")).toHaveClass("text-destructive/70");

    // A total-only cost still reaches the card, where mobile shows the cost.
    await user.click(screen.getByText("72%"));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "Total1,040,000$0.39",
    );
  });
});
