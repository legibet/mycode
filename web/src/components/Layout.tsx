/**
 * Root layout component.
 * Provides the base surface with subtle noise texture.
 */

import type { ReactNode } from "react";

import { cn } from "../utils/cn";

interface LayoutProps {
  children: ReactNode;
}

export function Layout({ children }: LayoutProps) {
  return (
    <div
      className={cn(
        "flex h-screen min-h-0 w-full flex-col overflow-hidden bg-background font-sans text-foreground antialiased supports-[height:100dvh]:h-dvh",
        "transition-colors duration-150",
      )}
    >
      {children}
    </div>
  );
}
