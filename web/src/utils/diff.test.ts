import { describe, expect, it } from "vitest";
import { hunksFromEdits, parsePatch } from "./diff";

describe("parsePatch", () => {
  it("numbers removed lines on the old side and the rest on the new side", () => {
    const patch = [
      "--- a/x.sql",
      "+++ b/x.sql",
      "@@ -9,4 +12,3 @@",
      " a",
      "--- old note",
      "-x",
      "+b",
      " c",
      "@@ -40,2 +43,3 @@",
      " d",
      "+e",
      " f",
      "\\ No newline at end of file",
      "",
    ].join("\n");

    expect(parsePatch(patch)).toEqual([
      [
        { kind: "context", text: "a", number: 12 },
        { kind: "remove", text: "-- old note", number: 10 },
        { kind: "remove", text: "x", number: 11 },
        { kind: "add", text: "b", number: 13 },
        { kind: "context", text: "c", number: 14 },
      ],
      [
        { kind: "context", text: "d", number: 43 },
        { kind: "add", text: "e", number: 44 },
        { kind: "context", text: "f", number: 45 },
      ],
    ]);
  });
});

describe("hunksFromEdits", () => {
  it("drops empty sides and the trailing newline of each text", () => {
    expect(
      hunksFromEdits([
        { oldText: "", newText: "x\r\ny\n" },
        { oldText: "", newText: "" },
      ]),
    ).toEqual([
      [
        { kind: "add", text: "x", number: null },
        { kind: "add", text: "y", number: null },
      ],
    ]);
  });
});
