/** Hover card for usage stats: label/value rows over a trigger text. */

import type { ReactNode } from "react";
import { cn } from "../../utils/cn";

export function StatsRow({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex justify-between gap-6">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">{value}</span>
    </span>
  );
}

/** Muted trigger text that reveals a stats card above on hover. */
export function StatsHover({
  trigger,
  align = "left",
  children,
}: {
  trigger: ReactNode;
  align?: "left" | "right";
  children: ReactNode;
}) {
  return (
    <span className="group/stats relative cursor-default text-xs tabular-nums text-muted-foreground/50 transition-colors duration-150 hover:text-muted-foreground/90">
      {trigger}
      <span
        role="tooltip"
        className={cn(
          "pointer-events-none absolute bottom-full z-10 mb-2 flex w-max flex-col gap-1 rounded-lg border border-border/50 bg-popover px-3.5 py-3 text-xs text-popover-foreground opacity-0 shadow-md transition-opacity delay-200 duration-150 group-hover/stats:opacity-100",
          align === "left" ? "left-0" : "right-0",
        )}
      >
        {children}
      </span>
    </span>
  );
}
