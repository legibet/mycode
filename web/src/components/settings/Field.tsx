import type { ReactNode } from "react";

import { cn } from "@/utils/cn";

interface FieldProps {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}

/** Labelled settings row. */
export function Field({ label, hint, children }: FieldProps) {
  // Desktop: 110px label column on the left, control + hint stacked on the right.
  // Mobile: everything stacked top-to-bottom because there's no horizontal room
  // for a label column on a 375px screen.
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5",
        "md:grid md:grid-cols-[110px_1fr] md:items-start md:gap-x-3 md:gap-y-0",
      )}
    >
      <div className="text-xs text-muted-foreground/80 select-none md:pt-2">{label}</div>
      <div className="flex min-w-0 flex-col gap-1.5">
        {children}
        {hint && <div className="text-xs leading-relaxed text-muted-foreground/70">{hint}</div>}
      </div>
    </div>
  );
}
