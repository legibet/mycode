/**
 * Inline divider rendered where the agent compacted the conversation.
 * `pending` renders the in-flight state with identical geometry, so the
 * divider settles in place when compaction finishes.
 */

import { Separator } from "@/components/ui/separator";
import { cn } from "@/utils/cn";

export function CompactMarker({ pending = false }: { pending?: boolean }) {
  return (
    <div className="flex items-center gap-3 px-2 py-1 select-none">
      <Separator className="flex-1 bg-border/40" />
      <span
        className={cn(
          "text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase",
          pending && "animate-pulse",
        )}
      >
        {pending ? "compacting…" : "compacted"}
      </span>
      <Separator className="flex-1 bg-border/40" />
    </div>
  );
}
