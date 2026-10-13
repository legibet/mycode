import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parsePatch } from "../../utils/diff";
import { EditDiff } from "./EditDiff";

describe("EditDiff", () => {
  it("keeps each row's own text once syntax tokens arrive", async () => {
    const lines = [
      "const a = 1;",
      "const b = 2;",
      "const c = 3;",
      "const d = 4;",
    ];
    const hunks = parsePatch(
      [
        "@@ -1,3 +1,3 @@",
        ` ${lines[0]}`,
        `-${lines[1]}`,
        `+${lines[2]}`,
        ` ${lines[3]}`,
      ].join("\n"),
    );
    const { container } = render(<EditDiff path="src/x.ts" hunks={hunks} />);

    const code = () =>
      Array.from(container.querySelectorAll(".shiki"), (el) => el.textContent);
    await waitFor(() =>
      expect(container.querySelector(".shiki span")).not.toBeNull(),
    );
    expect(code()).toEqual(lines);
  });
});
