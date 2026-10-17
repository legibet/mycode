import { cva } from "class-variance-authority";

const toggleVariants = cva(
  "group/toggle inline-flex items-center justify-center gap-1.5 rounded-sm text-xs font-medium whitespace-nowrap text-muted-foreground/80 transition-colors outline-none hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 data-pressed:bg-background data-pressed:text-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        default: "bg-transparent",
        outline: "border border-border/50 bg-transparent",
      },
      size: {
        default: "h-7 min-w-7 px-3",
        sm: "h-7 min-w-7 px-2.5",
        lg: "h-8 min-w-8 px-3",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export { toggleVariants };
