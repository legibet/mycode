/**
 * A turn's work: thinking, tool calls, interim text, automatic compaction.
 * It renders open while the turn runs and folds behind one summary row once
 * the turn finishes with an answer. No container: the row uses
 * ReasoningBlock's typography and the body its transition.
 */

import { ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";
import type { MessageBlock } from "../../types";
import { cn } from "../../utils/cn";
import { formatDuration } from "../../utils/format";

// Summary order puts side effects first; a null tool counts every other tool.
const TOOL_KINDS: readonly (readonly [
  tool: string | null,
  one: string,
  many: string,
])[] = [
  ["edit", "edit", "edits"],
  ["write", "write", "writes"],
  ["bash", "command", "commands"],
  ["read", "read", "reads"],
  ["websearch", "search", "searches"],
  ["webfetch", "fetch", "fetches"],
  [null, "tool call", "tool calls"],
];
const MAX_TOOL_SEGMENTS = 3;
// What the SDK records for a tool stopped by cancellation, not a failure.
const CANCELLED_OUTPUT = "error: cancelled";

function summarizeTools(blocks: MessageBlock[]) {
  const counts = TOOL_KINDS.map(() => 0);
  let failed = 0;
  for (const block of blocks) {
    if (block.type !== "tool_use") continue;
    const kind = TOOL_KINDS.findIndex(([tool]) => tool === block.name);
    const index = kind === -1 ? TOOL_KINDS.length - 1 : kind;
    counts[index] = (counts[index] ?? 0) + 1;
    if (
      block.runtime?.isError &&
      block.runtime.finalOutput !== CANCELLED_OUTPUT
    ) {
      failed += 1;
    }
  }

  const segments: string[] = [];
  let hiddenCalls = 0;
  for (const [index, [, one, many]] of TOOL_KINDS.entries()) {
    const count = counts[index] ?? 0;
    if (count === 0) continue;
    if (segments.length < MAX_TOOL_SEGMENTS) {
      segments.push(`${count} ${count === 1 ? one : many}`);
    } else {
      hiddenCalls += count;
    }
  }
  if (hiddenCalls > 0) segments.push(`+${hiddenCalls} more`);
  return { segments, failed };
}

interface WorkSectionProps {
  blocks: MessageBlock[];
  /** The turn finished with an answer: fold the work behind a summary row. */
  folded: boolean;
  durationMs?: number | undefined;
  children: ReactNode;
}

export function WorkSection({
  blocks,
  folded,
  durationMs,
  children,
}: WorkSectionProps) {
  const [expanded, setExpanded] = useState(false);
  const open = !folded || expanded;
  const [streamedHere] = useState(open);
  // A history turn mounts its work the first time it opens and keeps it, so
  // collapsing animates.
  const [mounted, setMounted] = useState(open);
  if (open && !mounted) setMounted(true);
  const { segments, failed } = summarizeTools(blocks);
  const lead =
    durationMs !== undefined
      ? `Worked for ${formatDuration(durationMs)}`
      : "Worked";

  return (
    <div data-work={open ? "open" : "folded"} className="group/work">
      {folded && (
        <button
          type="button"
          className={cn(
            "flex select-none items-center gap-1 text-left cursor-pointer",
            // The row fades in over the work folding beneath it.
            streamedHere &&
              "transition-opacity duration-300 starting:opacity-0",
          )}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="text-[12px] text-muted-foreground transition-colors duration-200 group-hover/work:text-foreground/80">
            {[lead, ...segments].join(" · ")}
            {failed > 0 && (
              <>
                {" · "}
                <span className="text-destructive/90">{failed} failed</span>
              </>
            )}
          </span>
          <ChevronRight
            aria-hidden="true"
            className={cn(
              "size-3 text-muted-foreground/60 transition-transform duration-200",
              expanded && "rotate-90",
            )}
          />
        </button>
      )}

      <div
        inert={!open}
        className={cn(
          "grid transition-[grid-template-rows,opacity] duration-300 ease-in-out",
          open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="overflow-hidden">
          {mounted && (
            <div className={cn("flex flex-col gap-3", folded && "pt-3")}>
              {children}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
