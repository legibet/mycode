import { Toggle as TogglePrimitive } from "@base-ui/react/toggle";
import { ToggleGroup as ToggleGroupPrimitive } from "@base-ui/react/toggle-group";
import { type VariantProps } from "class-variance-authority";
import * as React from "react";

import { toggleVariants } from "@/components/ui/toggle";
import { cn } from "@/utils/cn";

const ToggleGroupContext = React.createContext<VariantProps<typeof toggleVariants>>({
  size: "default",
  variant: "default",
});

/**
 * A segmented control. `value` is an array; for single selection pass `[value]`
 * and ignore an empty `onValueChange` result so the group can't be cleared.
 */
function ToggleGroup<Value extends string>({
  className,
  variant,
  size,
  children,
  ...props
}: ToggleGroupPrimitive.Props<Value> & VariantProps<typeof toggleVariants>) {
  return (
    <ToggleGroupPrimitive
      data-slot="toggle-group"
      data-variant={variant}
      data-size={size}
      className={cn(
        "group/toggle-group inline-flex w-fit items-center rounded-md border border-border/50 bg-muted/30 p-0.5 data-[orientation=vertical]:flex-col data-[orientation=vertical]:items-stretch",
        className,
      )}
      {...props}
    >
      <ToggleGroupContext.Provider value={{ variant, size }}>
        {children}
      </ToggleGroupContext.Provider>
    </ToggleGroupPrimitive>
  );
}

function ToggleGroupItem({
  className,
  children,
  variant = "default",
  size = "default",
  ...props
}: TogglePrimitive.Props & VariantProps<typeof toggleVariants>) {
  const context = React.useContext(ToggleGroupContext);

  return (
    <TogglePrimitive
      data-slot="toggle-group-item"
      data-variant={context.variant || variant}
      data-size={context.size || size}
      className={cn(
        "shrink-0 focus:z-10 focus-visible:z-10",
        toggleVariants({
          variant: context.variant || variant,
          size: context.size || size,
        }),
        className,
      )}
      {...props}
    >
      {children}
    </TogglePrimitive>
  );
}

export { ToggleGroup, ToggleGroupItem };
