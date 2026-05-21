import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownBlock, normalizeMathDelimiters } from "./MarkdownBlock";

describe("MarkdownBlock", () => {
  it("normalizes supported math delimiters", () => {
    expect(
      normalizeMathDelimiters(
        "Inline: $x + y$ and \\(a + b\\)\n\n$$x^2 + y^2$$\n\n\\[z^2\\]",
      ),
    ).toBe("Inline: $x + y$ and $a + b$\n\n$$\nx^2 + y^2\n$$\n\n$$\nz^2\n$$");
  });

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

  it("keeps code spans and code blocks unchanged", () => {
    const input = [
      "Inline formula: \\(x\\)",
      "",
      "Single tick: `\\(a\\)`",
      "",
      "Double tick: ``foo \\(b\\) bar``",
      "",
      "```js",
      'const s = "\\(c\\)"',
      "```",
      "",
      '    const t = "\\(d\\)"',
    ].join("\n");

    expect(normalizeMathDelimiters(input)).toBe(
      [
        "Inline formula: $x$",
        "",
        "Single tick: `\\(a\\)`",
        "",
        "Double tick: ``foo \\(b\\) bar``",
        "",
        "```js",
        'const s = "\\(c\\)"',
        "```",
        "",
        '    const t = "\\(d\\)"',
      ].join("\n"),
    );
  });

  it("leaves unclosed delimiters unchanged", () => {
    expect(normalizeMathDelimiters("oops \\(x and \\[y")).toBe(
      "oops \\(x and \\[y",
    );
  });
});
