/**
 * Root layout component.
 * Provides the base surface with subtle noise texture.
 */

import type { ReactNode } from 'react'
import { cn } from '../utils/cn'

interface LayoutProps {
  children: ReactNode
}

export function Layout({ children }: LayoutProps) {
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
