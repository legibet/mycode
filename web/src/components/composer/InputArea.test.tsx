import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";

import { InputArea, type InputAreaHandle } from "@/components/composer/InputArea";
import type { Cost } from "@/types";

const image = {
  id: "image-1",
  kind: "image" as const,
  data: "base64",
  mime_type: "image/png",
  name: "diagram.png",
  preview: "blob:diagram",
};

function restored(text: string, attachments = [image]) {
  return { id: `p-${text}`, submission: { text, workspaceFiles: [] }, attachments, input: [] };
}

function uploadedNames() {
  return screen
    .queryAllByRole("button", { name: /^Remove / })
    .map((button) => button.getAttribute("aria-label")?.slice("Remove ".length));
}

describe("InputArea", () => {
  it("rejects new unsupported media and keeps existing uploads while blocking submission", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const onSubmit = vi.fn().mockResolvedValue(true);
    const ref = createRef<InputAreaHandle>();

    render(
      <InputArea
        ref={ref}
        loading={false}
        onSubmit={onSubmit}
        onCancel={() => {}}
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

    expect(uploadedNames()).toEqual([]);
    expect(screen.getByText("Image unsupported")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "text-only" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "medium" })).toBeNull();

    // An image restored from an earlier model stays, but blocks the send.
    act(() => ref.current?.prepend([restored("")]));
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(onSubmit).not.toHaveBeenCalled());
    expect(screen.getByAltText("diagram.png")).toBeInTheDocument();
    expect(screen.getByText("Remove image or switch model")).toBeInTheDocument();
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
    expect(await screen.findByRole("dialog")).toHaveTextContent("Total1,040,000$0.39");
  });

  const runningProps = {
    loading: true,
    onCancel: () => {},
    config: {
      provider: "anthropic",
      model: "m",
      cwd: "/workspace",
      reasoningEfforts: {},
    },
    remoteConfig: null,
    onUpdateConfig: () => {},
  };

  it("offers Stop while running with an empty composer and sends with content", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(true);
    render(<InputArea {...runningProps} onSubmit={onSubmit} />);

    expect(screen.getByRole("button", { name: "Stop generating" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send message" })).toBeNull();

    await user.click(screen.getByRole("textbox"));
    await user.paste("use sqlite");
    const send = screen.getByRole("button", { name: "Send message" });

    await user.keyboard("{Control>}");
    await user.click(send);
    await user.keyboard("{/Control}");
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith({ text: "use sqlite", workspaceFiles: [] }, [], true),
    );
    expect(await screen.findByRole("button", { name: "Stop generating" })).toBeInTheDocument();
  });

  it("restores uploads ahead of the current ones and clears them once a send is accepted", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const ref = createRef<InputAreaHandle>();
    render(
      <InputArea {...runningProps} loading={false} ref={ref} supportsImages onSubmit={onSubmit} />,
    );

    await user.upload(
      screen.getByLabelText("Attach files"),
      new File(["notes"], "notes.md", { type: "text/markdown" }),
    );
    expect(await screen.findByRole("button", { name: "Remove notes.md" })).toBeInTheDocument();

    // A failed steer comes back: its text into the composer, its image ahead
    // of the upload already there, previewed from its data.
    act(() => ref.current?.prepend([restored("look at this")]));
    expect(uploadedNames()).toEqual(["diagram.png", "notes.md"]);
    expect(screen.getByAltText("diagram.png")).toHaveAttribute(
      "src",
      "data:image/png;base64,base64",
    );
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveTextContent("look at this"));

    const send = screen.getByRole("button", { name: "Send message" });
    await user.click(send);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0]?.[0]).toEqual({ text: "look at this", workspaceFiles: [] });
    expect(onSubmit.mock.calls[0]?.[1].map((file: { name: string }) => file.name)).toEqual([
      "diagram.png",
      "notes.md",
    ]);
    // Rejected: the uploads stay for another try.
    expect(uploadedNames()).toEqual(["diagram.png", "notes.md"]);

    await user.click(send);
    await waitFor(() => expect(uploadedNames()).toEqual([]));
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });

  it("drops uploads when the shown session changes and keeps the draft text", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <InputArea {...runningProps} loading={false} sessionId="s1" onSubmit={vi.fn()} />,
    );

    await user.click(screen.getByRole("textbox"));
    await user.paste("half-written");
    await user.upload(
      screen.getByLabelText("Attach files"),
      new File(["notes"], "notes.md", { type: "text/markdown" }),
    );
    expect(await screen.findByRole("button", { name: "Remove notes.md" })).toBeInTheDocument();

    rerender(<InputArea {...runningProps} loading={false} sessionId="s2" onSubmit={vi.fn()} />);
    expect(uploadedNames()).toEqual([]);
    expect(screen.getByRole("textbox")).toHaveTextContent("half-written");
  });

  it("keeps uploads added while an accepted send was in flight", async () => {
    const user = userEvent.setup();
    let accept!: (accepted: boolean) => void;
    const onSubmit = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          accept = resolve;
        }),
    );
    render(<InputArea {...runningProps} loading={false} onSubmit={onSubmit} />);
    const attach = screen.getByLabelText("Attach files");

    await user.upload(attach, new File(["a"], "sent.md", { type: "text/markdown" }));
    await screen.findByRole("button", { name: "Remove sent.md" });
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());

    await user.upload(attach, new File(["b"], "later.md", { type: "text/markdown" }));
    await screen.findByRole("button", { name: "Remove later.md" });
    await act(async () => accept(true));

    expect(uploadedNames()).toEqual(["later.md"]);
  });

  it("drops a file read that finishes after the session changed", async () => {
    const user = userEvent.setup();
    let finishRead!: () => void;
    const slow = new File(["notes"], "slow.md", { type: "text/markdown" });
    Object.defineProperty(slow, "arrayBuffer", {
      value: () =>
        new Promise<ArrayBuffer>((resolve) => {
          finishRead = () => resolve(new TextEncoder().encode("notes").buffer);
        }),
    });
    const { rerender } = render(
      <InputArea {...runningProps} loading={false} sessionId="s1" onSubmit={vi.fn()} />,
    );

    await user.upload(screen.getByLabelText("Attach files"), slow);
    rerender(<InputArea {...runningProps} loading={false} sessionId="s2" onSubmit={vi.fn()} />);
    await act(async () => finishRead());

    expect(uploadedNames()).toEqual([]);
  });

  it("lists queued messages and edits one only from an empty composer", async () => {
    const user = userEvent.setup();
    const onEditQueued = vi.fn();
    const onSteerQueued = vi.fn();
    const { rerender } = render(
      <InputArea
        {...runningProps}
        onSubmit={vi.fn()}
        queued={[
          {
            id: "q1",
            submission: {
              text: "then add tests\nand docs",
              workspaceFiles: [],
            },
            attachments: [{ id: "f1", kind: "text", name: "notes.md", text: "notes" }],
            input: [],
          },
        ]}
        onEditQueued={onEditQueued}
        onSteerQueued={onSteerQueued}
      />,
    );

    const list = screen.getByRole("list", { name: "Queued messages" });
    expect(list).toHaveTextContent("then add tests");
    expect(list).not.toHaveTextContent("and docs");
    expect(screen.getByTitle("1 attachment")).toHaveTextContent("1");

    await user.click(screen.getByRole("button", { name: "Steer" }));
    expect(onSteerQueued).toHaveBeenCalledWith("q1");

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEditQueued).toHaveBeenCalledWith("q1");

    await user.click(screen.getByRole("textbox"));
    await user.paste("draft");
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEditQueued).toHaveBeenCalledOnce();
    expect(screen.getByText("Clear the composer to edit")).toBeInTheDocument();

    // A partial item cannot come back whole, so it offers no Edit.
    rerender(
      <InputArea
        {...runningProps}
        onSubmit={vi.fn()}
        queued={[
          {
            id: "q2",
            submission: { text: "reloaded with a file", workspaceFiles: [] },
            attachments: [],
            input: [],
            partial: true,
          },
        ]}
      />,
    );
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("lists background commands with a Stop for each", async () => {
    const user = userEvent.setup();
    const onStopJob = vi.fn();
    render(
      <InputArea
        {...runningProps}
        loading={false}
        onSubmit={vi.fn()}
        jobs={[
          {
            tool_use_id: "call-1",
            label: "pytest -x",
            pid: 7,
            started_at: new Date().toISOString(),
          },
        ]}
        onStopJob={onStopJob}
      />,
    );

    const list = screen.getByRole("list", { name: "Background commands" });
    expect(list).toHaveTextContent("pytest -x");
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStopJob).toHaveBeenCalledWith("call-1");
  });

  it("shows no card when nothing is running or queued", () => {
    render(<InputArea {...runningProps} onSubmit={vi.fn()} jobs={[]} />);

    expect(screen.queryByRole("list", { name: "Background commands" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Queued messages" })).toBeNull();
  });
});
