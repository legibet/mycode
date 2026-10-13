/**
 * Mobile-only top bar with menu toggle, session title, and new chat.
 */

import { Menu, Plus } from "lucide-react";

interface MobileHeaderProps {
  title?: string | undefined;
  onMenuToggle: () => void;
  onCreateSession: () => void;
}

export function MobileHeader({ title, onMenuToggle, onCreateSession }: MobileHeaderProps) {
  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-border/40 bg-background px-4 md:hidden">
      <button
        type="button"
        aria-label="Toggle menu"
        onClick={onMenuToggle}
        className="-ml-2 flex size-10 items-center justify-center text-muted-foreground transition-[color,scale] duration-150 hover:text-foreground active:scale-95"
      >
        <Menu className="size-5" />
      </button>

      <span className="max-w-[60%] truncate text-center text-xs text-foreground/70">
        {title || "mycode"}
      </span>

      <button
        type="button"
        aria-label="New chat"
        onClick={onCreateSession}
        className="-mr-2 flex size-10 items-center justify-center text-muted-foreground transition-[color,scale] duration-150 hover:text-foreground active:scale-95"
      >
        <Plus className="size-5" />
      </button>
    </header>
  );
}
