/**
 * Mobile-only top bar with menu toggle, session title, and new chat.
 */

import { Menu, Plus } from 'lucide-react'

interface MobileHeaderProps {
  title?: string
  onMenuToggle: () => void
  onCreateSession: () => void
}

export function MobileHeader({
  title,
  onMenuToggle,
  onCreateSession,
}: MobileHeaderProps) {
  return (
    <header className="flex md:hidden h-12 shrink-0 items-center justify-between px-4 border-b border-border/40 bg-background">
      <button
        type="button"
        aria-label="Toggle menu"
        onClick={onMenuToggle}
        className="flex items-center justify-center h-10 w-10 -ml-2 text-muted-foreground hover:text-foreground active:scale-90 transition"
      >
        <Menu className="h-5 w-5" />
      </button>

      <span className="text-xs font-mono text-foreground/70 truncate max-w-[60%] text-center">
        {title || 'mycode'}
      </span>

      <button
        type="button"
        aria-label="New chat"
        onClick={onCreateSession}
        className="flex items-center justify-center h-10 w-10 -mr-2 text-muted-foreground hover:text-foreground active:scale-90 transition"
      >
        <Plus className="h-5 w-5" />
      </button>
    </header>
  )
}
