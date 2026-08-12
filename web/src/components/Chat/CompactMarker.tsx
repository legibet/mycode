/**
 * Inline divider rendered where the agent compacted the conversation.
 * `pending` renders the in-flight state with identical geometry, so the
 * divider settles in place when compaction finishes.
 */

import { cn } from "../../utils/cn";

export function CompactMarker({ pending = false }: { pending?: boolean }) {
  return (
    <div className="flex select-none items-center gap-3 px-2 py-1">
      <div className="h-px flex-1 bg-border/40" />
      <span
        className={cn(
          "text-[10px] uppercase tracking-[0.18em] text-muted-foreground/50",
          pending && "animate-pulse",
        )}
      >
        {pending ? "compacting…" : "compacted"}
      </span>
      <div className="h-px flex-1 bg-border/40" />
    </div>
  );
}
