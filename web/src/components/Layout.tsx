/**
 * Root layout component.
 * Provides the base surface with subtle noise texture.
 */

import { type ReactNode, useEffect } from 'react'
import { cn } from '../utils/cn'

interface LayoutProps {
  children: ReactNode
}

function useAppHeight() {
  useEffect(() => {
    const root = document.documentElement
    const viewport = window.visualViewport

    const updateHeight = () => {
      root.style.setProperty(
        '--app-height',
        `${viewport?.height ?? window.innerHeight}px`,
      )
    }

    updateHeight()

    viewport?.addEventListener('resize', updateHeight)
    viewport?.addEventListener('scroll', updateHeight)
    window.addEventListener('resize', updateHeight)

    return () => {
      viewport?.removeEventListener('resize', updateHeight)
      viewport?.removeEventListener('scroll', updateHeight)
      window.removeEventListener('resize', updateHeight)
      root.style.removeProperty('--app-height')
    }
  }, [])
}

export function Layout({ children }: LayoutProps) {
  useAppHeight()

  return (
    <div
      className={cn(
        'flex h-[var(--app-height)] min-h-0 w-full flex-col overflow-hidden bg-background font-sans text-foreground antialiased',
        'transition-colors duration-500',
      )}
    >
      {children}
    </div>
  )
}
