import type { ReactNode, RefObject } from "react";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useIsDesktop } from "@/hooks/useIsDesktop";
import { cn } from "@/utils/cn";

interface ResponsiveDialogProps {
  open: boolean;
  onClose: () => void;
  /** Read by screen readers only; the body draws its own header. */
  title: string;
  initialFocus?: RefObject<HTMLElement | null> | undefined;
  /** Desktop dialog size, e.g. `w-110 max-h-130`. */
  className?: string;
  /** On mobile, fill the screen instead of opening a bottom sheet. */
  fullScreen?: boolean;
  children: ReactNode;
}

/** A centered dialog on desktop, a bottom sheet on mobile. */
export function ResponsiveDialog({
  open,
  onClose,
  title,
  initialFocus,
  className,
  fullScreen = false,
  children,
}: ResponsiveDialogProps) {
  const isDesktop = useIsDesktop();
  const onOpenChange = (next: boolean) => {
    if (!next) onClose();
  };

  if (isDesktop) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          showCloseButton={false}
          initialFocus={initialFocus}
          className={cn(
            "flex max-w-[calc(100vw-2rem)] flex-col gap-0 p-0 sm:max-w-[calc(100vw-2rem)]",
            className,
          )}
        >
          <DialogTitle className="sr-only">{title}</DialogTitle>
          {children}
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        initialFocus={initialFocus}
        className={cn(
          "flex flex-col gap-0 p-0",
          fullScreen ? "h-dvh rounded-none" : "max-h-[82vh] rounded-t-2xl",
        )}
      >
        <SheetTitle className="sr-only">{title}</SheetTitle>
        {!fullScreen && (
          <div className="flex shrink-0 justify-center pt-2.5 pb-1" aria-hidden="true">
            <div className="h-0.75 w-10 rounded-full bg-border/60" />
          </div>
        )}
        {children}
      </SheetContent>
    </Sheet>
  );
}
