/**
 * Root layout component.
 * Provides the base surface with subtle noise texture.
 */

import { cn } from '../utils/cn'

export function Layout({ children }) {
  return (
    <div
      className={cn(
        'flex h-screen w-full flex-col bg-background font-sans text-foreground antialiased',
        'transition-colors duration-500',
      )}
    >
      {children}
    </div>
  )
}
