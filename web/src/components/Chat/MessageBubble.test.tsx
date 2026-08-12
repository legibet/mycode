import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageBubble } from "./MessageBubble";

const blocks = [{ type: "text" as const, text: "Done" }];

describe("turn stats card", () => {
  it("separates cached input and hides the cost column without details", () => {
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

    const detailed = screen.getByRole("tooltip");
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

    expect(screen.getByRole("tooltip")).not.toHaveTextContent("$");
    expect(screen.getByText("deepseek-v4-flash · $0.0027")).toBeInTheDocument();
  });

  it("distinguishes zero from a nonzero cost below display precision", () => {
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

    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "Input1$0.0000Output1<$0.0001Total2<$0.0001",
    );
    expect(screen.getByText("m · <$0.0001")).toBeInTheDocument();
  });
});
