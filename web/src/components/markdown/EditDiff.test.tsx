import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EditDiff } from "@/components/markdown/EditDiff";
import { parsePatch } from "@/utils/diff";

describe("EditDiff", () => {
  it("keeps each row's own text once syntax tokens arrive", async () => {
    const lines = ["const a = 1;", "const b = 2;", "const c = 3;", "const d = 4;"];
    const hunks = parsePatch(
      ["@@ -1,3 +1,3 @@", ` ${lines[0]}`, `-${lines[1]}`, `+${lines[2]}`, ` ${lines[3]}`].join(
        "\n",
      ),
    );
    const { container } = render(<EditDiff path="src/x.ts" hunks={hunks} />);

    const code = () => Array.from(container.querySelectorAll(".shiki"), (el) => el.textContent);
    await waitFor(() => expect(container.querySelector(".shiki span")).not.toBeNull());
    expect(code()).toEqual(lines);
  });

  it("starts each side from its own file's context", async () => {
    // An earlier edit turned the docstring opener into a comment: the shared
    // quotes close a docstring in the old file but open one in the new file.
    const hunks = parsePatch(
      ["@@ -2,2 +2,2 @@", '     """', "-    raw = 1", "+    raw = 2"].join("\n"),
      [
        {
          old: 'def load():\n    """Load config.',
          new: "def load():\n    # Load config.",
        },
      ],
    );
    const { container } = render(<EditDiff path="app.py" hunks={hunks} />);

    await waitFor(() => expect(container.querySelector(".shiki span")).not.toBeNull());
    const tokenCount = (row: number) =>
      container.querySelectorAll(".shiki")[row]?.querySelectorAll("span").length;
    expect(tokenCount(1)).toBeGreaterThan(1); // old side: code
    expect(tokenCount(2)).toBe(1); // new side: inside the string
  });
});
