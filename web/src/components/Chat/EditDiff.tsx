/**
 * Unified diff of an edit. Rows run edge to edge so the added/removed tint
 * spans the block. A single line-number column (the old number for a removed
 * line) sits before the marker. Long lines scroll instead of wrapping.
 */

import {
  type CSSProperties,
  Fragment,
  startTransition,
  useEffect,
  useState,
} from "react";
import type { ThemedToken } from "shiki/core";
import { cn } from "../../utils/cn";
import type { DiffHunk, DiffLine } from "../../utils/diff";

// Start the shiki chunk download at module-eval, not on first effect.
const highlighterModulePromise = import("../../utils/highlighter");

type LineTokens = ThemedToken[] | undefined;

const ROW_STYLE: Record<DiffLine["kind"], string> = {
  context: "",
  add: "diff-line-added",
  remove: "diff-line-removed",
};

const MARKER: Record<DiffLine["kind"], { glyph: string; className: string }> = {
  context: { glyph: " ", className: "" },
  add: { glyph: "+", className: "text-diff-added" },
  remove: { glyph: "-", className: "text-diff-removed" },
};

/** A file extension (or bare name, e.g. Dockerfile) doubles as a shiki language id. */
function languageForPath(path: string): string {
  const name = (path.split("/").pop() ?? "").toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1) : name;
}

/**
 * Highlight each side of the hunk as one run, so a construct spanning lines
 * (a string, a comment) keeps its colour, then map tokens back onto the rows.
 * Context lines take the new side's tokens.
 */
async function highlightHunk(
  hunk: DiffHunk,
  language: string,
): Promise<LineTokens[]> {
  const { highlightTokens } = await highlighterModulePromise;
  const side = (skip: DiffLine["kind"]) =>
    hunk
      .filter((line) => line.kind !== skip)
      .map((line) => line.text)
      .join("\n");
  const [oldTokens, newTokens] = await Promise.all([
    highlightTokens(side("add"), language),
    highlightTokens(side("remove"), language),
  ]);

  let oldIndex = 0;
  let newIndex = 0;
  return hunk.map((line) => {
    if (line.kind === "remove") return oldTokens?.[oldIndex++];
    if (line.kind === "context") oldIndex++;
    return newTokens?.[newIndex++];
  });
}

interface EditDiffProps {
  path: string;
  hunks: DiffHunk[];
}

export function EditDiff({ path, hunks }: EditDiffProps) {
  const language = languageForPath(path);
  const [highlight, setHighlight] = useState<{
    hunks: DiffHunk[];
    language: string;
    tokens: LineTokens[][];
  } | null>(null);

  useEffect(() => {
    // The render guard below ignores tokens for stale hunks/language.
    let cancelled = false;
    void Promise.all(hunks.map((hunk) => highlightHunk(hunk, language)))
      .then((tokens) => {
        if (cancelled) return;
        startTransition(() => setHighlight({ hunks, language, tokens }));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [hunks, language]);

  const tokens =
    highlight?.hunks === hunks && highlight.language === language
      ? highlight.tokens
      : null;

  const maxNumber = Math.max(
    0,
    ...hunks.flatMap((hunk) => hunk.map((line) => line.number ?? 0)),
  );
  // Without line numbers (edits not yet applied) the marker opens the row.
  const gutterStyle = maxNumber > 0 && {
    width: `${String(maxNumber).length}ch`,
  };

  return (
    <div className="rounded-md bg-code shadow-hairline overflow-hidden">
      <div className="max-h-60 overflow-auto scrollbar-subtle py-1.5 font-mono text-[13px] leading-normal">
        <div className="w-max min-w-full whitespace-pre">
          {hunks.map((hunk, h) => (
            <Fragment key={h}>
              {h > 0 && (
                <div
                  aria-hidden="true"
                  className="flex select-none text-muted-foreground/30"
                >
                  <span
                    className={cn(
                      "box-content shrink-0 text-center",
                      gutterStyle ? "pl-3 pr-2.5" : "w-[1ch] pl-3",
                    )}
                    style={gutterStyle || undefined}
                  >
                    ⋯
                  </span>
                </div>
              )}
              {hunk.map((line, i) => {
                const marker = MARKER[line.kind];
                const lineTokens = tokens?.[h]?.[i];
                return (
                  <div key={i} className={cn("flex", ROW_STYLE[line.kind])}>
                    {gutterStyle && (
                      <span
                        className="box-content shrink-0 select-none pl-3 pr-2.5 text-right tabular-nums text-muted-foreground/40"
                        style={gutterStyle}
                      >
                        {line.number}
                      </span>
                    )}
                    <span
                      className={cn(
                        "w-[2ch] shrink-0 select-none",
                        !gutterStyle && "box-content pl-3",
                        marker.className,
                      )}
                    >
                      {marker.glyph}
                    </span>
                    <span className="shiki pr-3">
                      {lineTokens
                        ? lineTokens.map((token, t) => (
                            <span
                              key={t}
                              style={token.htmlStyle as CSSProperties}
                            >
                              {token.content}
                            </span>
                          ))
                        : line.text}
                    </span>
                  </div>
                );
              })}
            </Fragment>
          ))}
        </div>
      </div>
    </div>
  );
}
