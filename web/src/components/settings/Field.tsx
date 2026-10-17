import { type ReactNode, useId } from "react";

import { cn } from "@/utils/cn";

interface FieldProps {
  label: string;
  hint?: ReactNode;
  /** The field holds several controls, so the label names them as a group
   * through aria-labelledby instead of pointing at one control. */
  group?: boolean;
  /** Gets the id for the control, or for the group's aria-labelledby. */
  children: (id: string) => ReactNode;
}

/** Labelled settings row. */
export function Field({ label, hint, group = false, children }: FieldProps) {
  const id = useId();
  const labelClass = "text-xs text-muted-foreground/80 select-none md:pt-2";
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
      {group ? (
        <span id={id} className={labelClass}>
          {label}
        </span>
      ) : (
        <label htmlFor={id} className={labelClass}>
          {label}
        </label>
      )}
      <div className="flex min-w-0 flex-col gap-1.5">
        {children(id)}
        {hint && <div className="text-xs leading-relaxed text-muted-foreground/70">{hint}</div>}
      </div>
    </div>
  );
}
