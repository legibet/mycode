export interface DiffLine {
  kind: "context" | "add" | "remove";
  text: string;
  /** New-file line number, or the old one for a removed line; null when unknown. */
  number: number | null;
}

export type DiffHunk = DiffLine[];

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Parse a single-file unified diff (the edit tool's `metadata.patch`).
 * File headers before the first hunk and `\ No newline` markers are skipped.
 */
export function parsePatch(patch: string): DiffHunk[] {
  const hunks: DiffHunk[] = [];
  let hunk: DiffHunk | null = null;
  let oldNumber = 0;
  let newNumber = 0;

  for (const line of patch.split("\n")) {
    const header = HUNK_HEADER_RE.exec(line);
    if (header) {
      hunk = [];
      hunks.push(hunk);
      oldNumber = Number(header[1]);
      newNumber = Number(header[2]);
      continue;
    }
    if (!hunk) continue;

    const text = line.slice(1);
    switch (line[0]) {
      case " ":
        hunk.push({ kind: "context", text, number: newNumber });
        oldNumber++;
        newNumber++;
        break;
      case "-":
        hunk.push({ kind: "remove", text, number: oldNumber++ });
        break;
      case "+":
        hunk.push({ kind: "add", text, number: newNumber++ });
        break;
    }
  }

  return hunks;
}

function splitLines(text: string): string[] {
  return text ? text.replace(/\r?\n$/, "").split(/\r?\n/) : [];
}

/** One hunk per edit, without line numbers: the view before the patch exists. */
export function hunksFromEdits(
  edits: { oldText: string; newText: string }[],
): DiffHunk[] {
  return edits
    .map((edit) => [
      ...splitLines(edit.oldText).map(
        (text): DiffLine => ({ kind: "remove", text, number: null }),
      ),
      ...splitLines(edit.newText).map(
        (text): DiffLine => ({ kind: "add", text, number: null }),
      ),
    ])
    .filter((hunk) => hunk.length > 0);
}
