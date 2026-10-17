import * as React from "react";

import { cn } from "@/utils/cn";

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex field-sizing-content min-h-16 w-full rounded-lg border border-border/60 bg-transparent px-2.5 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground/50 hover:border-border focus:border-primary/60 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive/70 aria-invalid:focus:border-destructive md:text-sm",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
