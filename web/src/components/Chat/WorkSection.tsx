/**
 * A turn's work: thinking, tool calls, interim text, automatic compaction.
 * It renders open while the turn runs and folds behind one summary row once
 * the turn ends. No container: the row uses ReasoningBlock's typography and
 * the body its transition.
 */

import { ChevronRight } from "lucide-react";
import { type ReactNode, useLayoutEffect, useRef, useState } from "react";
import type { Interruption, MessageBlock } from "../../types";
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
// What the SDK records for a tool stopped by cancellation, not a failure.
const CANCELLED_OUTPUT = "error: cancelled";

function summarizeTools(blocks: MessageBlock[]): string {
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
  for (const [index, [, one, many]] of TOOL_KINDS.entries()) {
    const count = counts[index] ?? 0;
    if (count > 0) segments.push(`${count} ${count === 1 ? one : many}`);
  }
  if (failed > 0) segments.push(`${failed} failed`);
  return segments.join(" · ");
}

interface SummaryRowProps {
  lead: string;
  tools: string;
  failed: boolean;
  expanded: boolean;
  fadeIn: boolean;
  onToggle: () => void;
}

/**
 * One line: the lead, then the tool counts whole or not at all. The counts
 * drop when the full row would not fit, keeping the chevron beside the text.
 */
function SummaryRow({
  lead,
  tools,
  failed,
  expanded,
  fadeIn,
  onToggle,
}: SummaryRowProps) {
  const rowRef = useRef<HTMLButtonElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const fullRef = useRef<HTMLSpanElement>(null);
  const [toolsFit, setToolsFit] = useState(true);
  const full = tools ? `${lead} · ${tools}` : lead;

  useLayoutEffect(() => {
    const row = rowRef.current;
    const text = textRef.current;
    const fullText = fullRef.current;
    const space = row?.parentElement;
    if (!row || !text || !fullText || !space) return;
    const measure = () => {
      const chevron = row.offsetWidth - text.offsetWidth;
      setToolsFit(fullText.offsetWidth + chevron <= space.clientWidth);
    };
    measure();
    // The row's space and its full text each change on their own.
    const observer = new ResizeObserver(measure);
    observer.observe(space);
    observer.observe(fullText);
    return () => observer.disconnect();
  }, []);

  return (
    <button
      ref={rowRef}
      type="button"
      className={cn(
        "relative flex max-w-full select-none items-center gap-1 overflow-hidden text-left text-[12px] cursor-pointer",
        // The row fades in over the work folding beneath it.
        fadeIn && "transition-opacity duration-300 starting:opacity-0",
      )}
      aria-label={full}
      aria-expanded={expanded}
      onClick={onToggle}
    >
      <span
        ref={textRef}
        className="min-w-0 overflow-hidden whitespace-pre text-muted-foreground transition-colors duration-200 group-hover/work:text-foreground/80"
      >
        <span className={cn(failed && "text-destructive/90")}>{lead}</span>
        {tools && toolsFit && ` · ${tools}`}
      </span>
      {/* Measures the full row, laid out but never shown. */}
      <span
        ref={fullRef}
        aria-hidden="true"
        className="invisible absolute top-0 left-0 whitespace-pre"
      >
        {full}
      </span>
      <ChevronRight
        aria-hidden="true"
        className={cn(
          "size-3 shrink-0 text-muted-foreground/60 transition-transform duration-200",
          expanded && "rotate-90",
        )}
      />
    </button>
  );
}

interface WorkSectionProps {
  blocks: MessageBlock[];
  /** The turn ended: fold the work behind a summary row. */
  folded: boolean;
  /** How the turn ended early; the row leads with it in place of the time. */
  interruption?: Interruption | undefined;
  durationMs?: number | undefined;
  children: ReactNode;
}

export function WorkSection({
  blocks,
  folded,
  interruption,
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
  const tools = summarizeTools(blocks);
  // An interrupted turn's time runs only to its last completed response.
  const lead =
    interruption === "cancelled"
      ? "Stopped"
      : interruption === "error"
        ? "Failed"
        : durationMs !== undefined
          ? `Worked for ${formatDuration(durationMs)}`
          : "Worked";

  return (
    <div data-work={open ? "open" : "folded"} className="group/work">
      {folded && (
        <SummaryRow
          lead={lead}
          tools={tools}
          failed={interruption === "error"}
          expanded={expanded}
          fadeIn={streamedHere}
          onToggle={() => setExpanded(!expanded)}
        />
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
