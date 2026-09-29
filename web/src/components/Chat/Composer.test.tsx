import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { Composer, type ComposerHandle } from "./Composer";

function renderWithHistory(history: string[]) {
  const composerRef = createRef<ComposerHandle>();
  const onSubmit = vi.fn().mockResolvedValue(true);
  render(
    <Composer
      ref={composerRef}
      disabled={false}
      placeholder="Message…"
      loading={false}
      cwd="/workspace"
      supportsImages
      supportsDocuments
      skills={[]}
      hasUploads={false}
      history={history}
      onSubmit={onSubmit}
      onPasteFiles={() => {}}
      onHasContentChange={() => {}}
    />,
  );
  return { editor: screen.getByRole("textbox"), composerRef, onSubmit };
}

describe("Composer", () => {
  it("submits a selected workspace file and keeps it when rejected", async () => {
    const user = userEvent.setup();
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          entries: [
            {
              name: "main.ts",
              path: "src/main.ts",
              kind: "text",
            },
          ],
          truncated: false,
          error: "",
        }),
      ),
    );
    const composerRef = createRef<ComposerHandle>();
    let resolveSubmission: ((accepted: boolean) => void) | undefined;
    const onSubmit = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveSubmission = resolve;
        }),
    );

    render(
      <Composer
        ref={composerRef}
        disabled={false}
        placeholder="Message…"
        loading={false}
        cwd="/workspace"
        supportsImages
        supportsDocuments
        skills={[]}
        hasUploads={false}
        history={[]}
        onSubmit={onSubmit}
        onPasteFiles={() => {}}
        onHasContentChange={() => {}}
      />,
    );

    const editor = screen.getByRole("textbox");
    await user.click(editor);
    await user.paste("review @src/ma");
    await user.click(await screen.findByRole("option", { name: /main\.ts/ }));

    expect(editor).toHaveTextContent("review @src/main.ts");
    composerRef.current?.submit(false);

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenCalledWith(
      {
        text: "review @src/main.ts ",
        workspaceFiles: [
          { path: "src/main.ts", name: "main.ts", kind: "text" },
        ],
      },
      false,
    );

    await act(async () => resolveSubmission?.(false));

    expect(editor).toHaveTextContent("review @src/main.ts");
  });

  it("completes a skill inside the message and submits the visible text", async () => {
    const user = userEvent.setup();
    const composerRef = createRef<ComposerHandle>();
    const onSubmit = vi.fn().mockResolvedValue(true);

    render(
      <Composer
        ref={composerRef}
        disabled={false}
        placeholder="Message…"
        loading={false}
        cwd="/workspace"
        supportsImages
        supportsDocuments
        skills={[{ name: "ui", description: "Design user interfaces." }]}
        hasUploads={false}
        history={[]}
        onSubmit={onSubmit}
        onPasteFiles={() => {}}
        onHasContentChange={() => {}}
      />,
    );

    const editor = screen.getByRole("textbox");
    await user.click(editor);
    await user.paste("Please use /u");
    await user.click(await screen.findByRole("option", { name: /\/ui/ }));
    await user.paste("for this page");

    composerRef.current?.submit(false);
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenCalledWith(
      {
        text: "Please use /ui for this page",
        workspaceFiles: [],
      },
      false,
    );
  });

  it("walks prompt history with arrow keys from an empty editor", async () => {
    const user = userEvent.setup();
    const { editor, composerRef, onSubmit } = renderWithHistory([
      "first prompt",
      "second\nline",
    ]);
    await user.click(editor);

    await user.keyboard("{ArrowUp}");
    expect(editor).toHaveTextContent("secondline");
    await user.keyboard("{ArrowUp}");
    expect(editor).toHaveTextContent("first prompt");
    await user.keyboard("{ArrowUp}");
    expect(editor).toHaveTextContent("first prompt");

    await user.keyboard("{ArrowDown}");
    expect(editor).toHaveTextContent("secondline");
    await user.keyboard("{ArrowDown}");
    expect(editor).toHaveTextContent("");

    // Recalled multi-line text submits with its line break intact.
    await user.keyboard("{ArrowUp}");
    composerRef.current?.submit(false);
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        {
          text: "second\nline",
          workspaceFiles: [],
        },
        false,
      ),
    );
  });

  it("keeps a draft when ArrowUp is pressed in a non-empty editor", async () => {
    const user = userEvent.setup();
    const { editor } = renderWithHistory(["first prompt"]);
    await user.click(editor);
    await user.paste("draft");

    await user.keyboard("{ArrowUp}");
    expect(editor).toHaveTextContent("draft");
    expect(editor).not.toHaveTextContent("first prompt");
  });

  it("reports Mod+Enter and submits built-in commands as text while running", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(true);
    render(
      <Composer
        disabled={false}
        placeholder="Message…"
        loading
        cwd="/workspace"
        supportsImages
        supportsDocuments
        skills={[]}
        hasUploads={false}
        history={[]}
        onSubmit={onSubmit}
        onSlashCommand={() => {}}
        onPasteFiles={() => {}}
        onHasContentChange={() => {}}
      />,
    );
    const editor = screen.getByRole("textbox");
    await user.click(editor);

    await user.paste("use sqlite");
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(onSubmit).toHaveBeenLastCalledWith(
        { text: "use sqlite", workspaceFiles: [] },
        false,
      ),
    );

    await user.paste("then add tests");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() =>
      expect(onSubmit).toHaveBeenLastCalledWith(
        { text: "then add tests", workspaceFiles: [] },
        true,
      ),
    );

    // Built-in slash commands stay idle-only: Enter submits the text.
    await user.paste("/new");
    expect(screen.queryByRole("listbox")).toBeNull();
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(onSubmit).toHaveBeenLastCalledWith(
        { text: "/new", workspaceFiles: [] },
        false,
      ),
    );
  });

  it("puts submissions back ahead of the draft with their pills", async () => {
    const user = userEvent.setup();
    const { editor, composerRef, onSubmit } = renderWithHistory([]);
    await user.click(editor);
    await user.paste("draft");

    act(() =>
      composerRef.current?.prepend([
        {
          text: "read @src/a.ts",
          workspaceFiles: [{ path: "src/a.ts", name: "a.ts", kind: "text" }],
        },
        { text: "and this", workspaceFiles: [] },
      ]),
    );
    await waitFor(() =>
      expect(editor.querySelector("[data-workspace-file]")).toHaveTextContent(
        "@src/a.ts",
      ),
    );

    composerRef.current?.submit(false);
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        {
          text: "read @src/a.ts\n\nand this\n\ndraft",
          workspaceFiles: [{ path: "src/a.ts", name: "a.ts", kind: "text" }],
        },
        false,
      ),
    );
  });
});
