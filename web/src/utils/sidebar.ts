/**
 * Sidebar width constants and clamping.
 * Desktop only — mobile uses a fixed overlay width.
 */

export const SIDEBAR_DEFAULT_WIDTH = 256;
export const SIDEBAR_MIN_WIDTH = 200;
export const SIDEBAR_MAX_WIDTH = 480;

// Never let the sidebar eat more than 40% of the viewport.
export function getMaxSidebarWidth(): number {
  if (typeof window === "undefined") return SIDEBAR_MAX_WIDTH;
  return Math.min(SIDEBAR_MAX_WIDTH, Math.floor(window.innerWidth * 0.4));
}

export function clampSidebarWidth(width: number): number {
  return Math.max(SIDEBAR_MIN_WIDTH, Math.min(getMaxSidebarWidth(), width));
}
