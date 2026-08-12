import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownBlock } from "./MarkdownBlock";

describe("MarkdownBlock", () => {
  it("renders inline and display math", () => {
    const { container } = render(
      <MarkdownBlock
        content={"Inline $x$ and \\(y\\)\n\n$$z^2$$\n\n\\[w^2\\]"}
      />,
    );

    expect(container.querySelectorAll(".katex").length).toBeGreaterThanOrEqual(
      4,
    );
    expect(container.querySelectorAll(".katex-display")).toHaveLength(2);
  });

  it("renders single-dollar math without consuming currency", () => {
    const { container } = render(
      <MarkdownBlock
        content={[
          "Price range: $10 to $20. Formula: $x + y$.",
          "",
          "| Input |",
          "| --- |",
          "| $1 → **$0.20** |",
        ].join("\n")}
      />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(1);
    expect(container.textContent).toContain("Price range: $10 to $20");
    expect(container.querySelector("strong")?.textContent).toBe("$0.20");
    expect(container.textContent).toContain("$1 → $0.20");
  });

  it("renders emphasis around punctuation adjacent to CJK text", () => {
    const { container } = render(
      <MarkdownBlock
        content={'而是**"引号内容"**——尾部，以及**“弯引号”**和*"斜体"*'}
      />,
    );

    const [double, curly] = container.querySelectorAll("strong");
    expect(double?.textContent).toBe('"引号内容"');
    expect(curly?.textContent).toBe("“弯引号”");
    expect(container.querySelector("em")?.textContent).toBe('"斜体"');
  });

  it("keeps math syntax literal in code and incomplete expressions", () => {
    const { container } = render(
      <MarkdownBlock
        content={[
          "Inline code: `$x$`",
          "",
          "```text",
          "\\(y\\)",
          "```",
          "",
          "Unfinished: $z",
        ].join("\n")}
      />,
    );

    expect(container.querySelectorAll(".katex")).toHaveLength(0);
    expect(container.textContent).toContain("$x$");
    expect(container.textContent).toContain("\\(y\\)");
    expect(container.textContent).toContain("Unfinished: $z");
  });
});
