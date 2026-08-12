/** Format a USD cost without rounding a nonzero value to zero. */
export function formatCost(cost: number): string {
  if (cost === 0) return "$0.0000";
  if (cost < 0.0001) return "<$0.0001";
  return `$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}`;
}
