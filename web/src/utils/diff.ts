export interface DiffLine {
  kind: "context" | "add" | "remove";
  text: string;
  /** New-file line number, or the old one for a removed line; null when unknown. */
  number: number | null;
}

/** Each file's text just before a hunk, to start highlighting in the right state. */
export interface HunkContext {
  old: string;
  new: string;
}

export interface DiffHunk {
  lines: DiffLine[];
  context: HunkContext;
}

const NO_CONTEXT: HunkContext = { old: "", new: "" };

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Parse a single-file unified diff (the edit tool's `metadata.patch`), pairing
 * each hunk with its entry in `metadata.highlight_context`.
 * File headers before the first hunk and `\ No newline` markers are skipped.
 */
export function parsePatch(patch: string, contexts: readonly HunkContext[] = []): DiffHunk[] {
  const hunks: DiffHunk[] = [];
  let lines: DiffLine[] | null = null;
  let oldNumber = 0;
  let newNumber = 0;

  for (const line of patch.split("\n")) {
    const header = HUNK_HEADER_RE.exec(line);
    if (header) {
      lines = [];
      hunks.push({ lines, context: contexts[hunks.length] ?? NO_CONTEXT });
      oldNumber = Number(header[1]);
      newNumber = Number(header[2]);
      continue;
    }
    if (!lines) continue;

    const text = line.slice(1);
    switch (line[0]) {
      case " ":
        lines.push({ kind: "context", text, number: newNumber });
        oldNumber++;
        newNumber++;
        break;
      case "-":
        lines.push({ kind: "remove", text, number: oldNumber++ });
        break;
      case "+":
        lines.push({ kind: "add", text, number: newNumber++ });
        break;
    }
  }

  return hunks;
}

function splitLines(text: string): string[] {
  return text ? text.replace(/\r?\n$/, "").split(/\r?\n/) : [];
}

/** One hunk per edit, without line numbers: the view before the patch exists. */
export function hunksFromEdits(edits: { oldText: string; newText: string }[]): DiffHunk[] {
  return edits
    .map((edit) => ({
      lines: [
        ...splitLines(edit.oldText).map((text): DiffLine => ({
          kind: "remove",
          text,
          number: null,
        })),
        ...splitLines(edit.newText).map((text): DiffLine => ({ kind: "add", text, number: null })),
      ],
      context: NO_CONTEXT,
    }))
    .filter((hunk) => hunk.lines.length > 0);
}
