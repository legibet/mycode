/**
 * Mobile-only top bar with menu toggle, session title, and new chat.
 */

import { Menu, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";

interface MobileHeaderProps {
  title?: string | undefined;
  onMenuToggle: () => void;
  onCreateSession: () => void;
}

export function MobileHeader({ title, onMenuToggle, onCreateSession }: MobileHeaderProps) {
  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-border/40 bg-background px-4 md:hidden">
      <Button
        variant="ghost"
        size="icon-lg"
        aria-label="Toggle menu"
        onClick={onMenuToggle}
        className="-ml-2 size-10"
      >
        <Menu className="size-5" />
      </Button>

      <span className="max-w-[60%] truncate text-center text-xs text-foreground/70">
        {title || "mycode"}
      </span>

      <Button
        variant="ghost"
        size="icon-lg"
        aria-label="New chat"
        onClick={onCreateSession}
        className="-mr-2 size-10"
      >
        <Plus className="size-5" />
      </Button>
    </header>
  );
}
