import { ChevronDownIcon } from "lucide-react";
import * as React from "react";

import { cn } from "@/utils/cn";

type NativeSelectProps = Omit<React.ComponentProps<"select">, "size"> & {
  size?: "sm" | "default";
};

function NativeSelect({ className, size = "default", ...props }: NativeSelectProps) {
  return (
    <div
      className={cn(
        "group/native-select relative inline-flex w-full min-w-0 has-[select:disabled]:opacity-50",
        className,
      )}
      data-slot="native-select-wrapper"
      data-size={size}
    >
      <select
        data-slot="native-select"
        data-size={size}
        className="h-9 w-full min-w-0 appearance-none border-b border-border/60 bg-transparent pr-7 pl-2 text-ui transition-colors outline-none select-none hover:border-border focus:border-primary disabled:pointer-events-none disabled:cursor-not-allowed aria-invalid:border-destructive/70 aria-invalid:focus:border-destructive data-[size=sm]:h-7"
        {...props}
      />
      <ChevronDownIcon
        className="pointer-events-none absolute top-1/2 right-2 size-3 -translate-y-1/2 text-muted-foreground/60 select-none"
        aria-hidden="true"
        data-slot="native-select-icon"
      />
    </div>
  );
}

function NativeSelectOption({ className, ...props }: React.ComponentProps<"option">) {
  return (
    <option
      data-slot="native-select-option"
      className={cn("bg-[Canvas] text-[CanvasText]", className)}
      {...props}
    />
  );
}

export { NativeSelect, NativeSelectOption };
