import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Interruption, MessageBlock } from "../../types";
import { MessageBubble } from "./MessageBubble";

const blocks = [{ type: "text" as const, text: "Done" }];

describe("turn stats card", () => {
  it("separates cached input and prices only the total without details", async () => {
    const { rerender } = render(
      // biome-ignore lint/a11y/useValidAriaRole: component prop is the message role
      <MessageBubble
        role="assistant"
        blocks={blocks}
        isLoading={false}
        model="deepseek-v4-flash"
        stats={{
          total_tokens: 354_561,
          input_tokens: 350_226,
          cache_read_tokens: 346_624,
          output_tokens: 4_335,
          cost: {
            input: 0.0005,
            cache_read: 0.001,
            output: 0.0012,
            total: 0.0027,
          },
        }}
      />,
    );

    fireEvent.click(screen.getByText("deepseek-v4-flash · $0.0027"));
    const detailed = await screen.findByRole("dialog");
    expect(detailed).toHaveTextContent("Input3,602$0.0005");
    expect(detailed).toHaveTextContent("Cache read346,624$0.0010");
    expect(detailed).toHaveTextContent("Output4,335$0.0012");
    expect(detailed).toHaveTextContent("Total354,561$0.0027");

    rerender(
      // biome-ignore lint/a11y/useValidAriaRole: component prop is the message role
      <MessageBubble
        role="assistant"
        blocks={blocks}
        isLoading={false}
        model="deepseek-v4-flash"
        stats={{
          total_tokens: 354_561,
          input_tokens: 350_226,
          cache_read_tokens: 346_624,
          output_tokens: 4_335,
          cost: { total: 0.0027 },
        }}
      />,
    );

    // A total-only cost keeps the token rows and prices only the Total row.
    const totalOnly = screen.getByRole("dialog");
    expect(totalOnly).toHaveTextContent("Input3,602Cache read");
    expect(totalOnly).toHaveTextContent("Total354,561$0.0027");
    expect(screen.getByText("deepseek-v4-flash · $0.0027")).toBeInTheDocument();
  });

  it("distinguishes zero from a nonzero cost below display precision", async () => {
    render(
      // biome-ignore lint/a11y/useValidAriaRole: component prop is the message role
      <MessageBubble
        role="assistant"
        blocks={blocks}
        isLoading={false}
        model="m"
        stats={{
          total_tokens: 2,
          input_tokens: 1,
          output_tokens: 1,
          cost: { input: 0, output: 0.00000014, total: 0.00000014 },
        }}
      />,
    );

    fireEvent.click(screen.getByText("m · <$0.0001"));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "Input1$0.0000Output1<$0.0001Total2<$0.0001",
    );
    expect(screen.getByText("m · <$0.0001")).toBeInTheDocument();
  });
});

describe("turn work folding", () => {
  const turn: MessageBlock[] = [
    {
      type: "tool_use",
      id: "t1",
      name: "read",
      input: { path: "a.py" },
      renderKey: "t1",
      runtime: {
        pending: false,
        output: "",
        finalOutput: "1 line",
        metadata: null,
        isError: false,
      },
    },
    { type: "text", text: "The answer", renderKey: "answer" },
  ];

  function renderTurn(props: {
    isStreaming: boolean;
    blocks?: MessageBlock[];
    interruption?: Interruption;
    error?: string;
  }) {
    return (
      // biome-ignore lint/a11y/useValidAriaRole: component prop is the message role
      <MessageBubble
        role="assistant"
        blocks={props.blocks ?? turn}
        isLoading={props.isStreaming}
        isStreaming={props.isStreaming}
        interruption={props.interruption}
        error={props.error}
        stats={{ duration_ms: 23_000 }}
      />
    );
  }

  it("folds the work once the turn ends, keeping the answer and tool state", () => {
    const { rerender } = render(renderTurn({ isStreaming: true }));
    const answer = screen.getByText("The answer");
    const tool = screen.getByRole("button", { name: /read/ });
    fireEvent.click(tool);
    expect(screen.queryByText(/Worked/)).toBeNull();

    rerender(renderTurn({ isStreaming: false }));

    expect(
      screen.getByRole("button", { name: "Worked for 23s · 1 read" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("The answer")).toBe(answer);
    expect(tool.isConnected).toBe(true);
    expect(tool).toHaveAttribute("aria-expanded", "true");
  });

  it("folds a stopped turn without an answer behind its status", () => {
    render(
      renderTurn({
        isStreaming: false,
        blocks: turn.slice(0, 1),
        interruption: "cancelled",
      }),
    );

    expect(
      screen.getByRole("button", { name: "Stopped · 1 read" }),
    ).toHaveAttribute("aria-expanded", "false");
    // The row carries the stop; no separate line repeats it.
    expect(screen.getAllByText("Stopped")).toHaveLength(1);
  });

  it("folds a failed turn and keeps its error outside the fold", () => {
    render(
      renderTurn({
        isStreaming: false,
        interruption: "error",
        error: "HTTP 500",
      }),
    );

    expect(
      screen.getByRole("button", { name: "Failed · 1 read" }),
    ).toBeInTheDocument();
    expect(screen.getByText("HTTP 500")).toBeInTheDocument();
  });

  it("marks a stopped turn that has no work to fold", () => {
    render(
      renderTurn({
        isStreaming: false,
        blocks: turn.slice(1),
        interruption: "cancelled",
      }),
    );

    expect(screen.queryByRole("button", { name: /Stopped/ })).toBeNull();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });

  it("shows every tool kind and failures, with no cap", () => {
    const tool = (id: string, name: string, isError = false): MessageBlock => ({
      type: "tool_use",
      id,
      name,
      input: {},
      renderKey: id,
      runtime: {
        pending: false,
        output: "",
        finalOutput: isError ? "error: exit 1" : "ok",
        metadata: null,
        isError,
      },
    });
    render(
      renderTurn({
        isStreaming: false,
        blocks: [
          tool("a", "edit"),
          tool("b", "bash", true),
          tool("c", "read"),
          tool("d", "websearch"),
          ...turn.slice(1),
        ],
      }),
    );

    expect(
      screen.getByRole("button", {
        name: "Worked for 23s · 1 edit · 1 command · 1 read · 1 search · 1 failed",
      }),
    ).toBeInTheDocument();
  });
});

describe("run error row", () => {
  it("renders the error as plain text, not markdown", () => {
    const error = 'provider said **no** to {"model": "x_y"}';
    render(
      // biome-ignore lint/a11y/useValidAriaRole: component prop is the message role
      <MessageBubble
        role="assistant"
        blocks={[{ type: "text", text: "Partial" }]}
        isLoading={false}
        interruption="error"
        error={error}
      />,
    );

    expect(screen.getByText(error)).toBeInTheDocument();
  });
});
