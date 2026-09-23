/** Format a USD cost without rounding a nonzero value to zero. */
export function formatCost(cost: number): string {
  if (cost === 0) return "$0.0000";
  if (cost < 0.0001) return "<$0.0001";
  return `$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`;
}

/** Format an elapsed time in whole seconds: `8s`, `2m 5s`, `1h 3m`. */
export function formatDuration(ms: number): string {
  const secs = Math.max(1, Math.round(ms / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return secs % 60 ? `${mins}m ${secs % 60}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  return mins % 60 ? `${hours}h ${mins % 60}m` : `${hours}h`;
}

export function prettifyPath(path: string): string {
  const home = path.match(/^(\/Users\/[^/]+|\/home\/[^/]+)(.*)$/);
  return home ? `~${home[2] ?? ""}` : path;
}
