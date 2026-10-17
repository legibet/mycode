import { hasUsageRows, StatsPopover, StatsText, UsageGrid } from "@/components/messages/StatsCard";
import type { UsageTotals } from "@/types";
import { formatCost } from "@/utils/format";

/** Session state cluster: `27% · $0.42`, context and session usage in a card.
 * Mobile keeps only the context percentage; the card still has the cost. */
export function SessionStats({
  currentContext,
  sessionUsage,
  compactThreshold,
}: {
  currentContext: { tokens: number; window: number } | null;
  sessionUsage: UsageTotals | null;
  compactThreshold: number | undefined;
}) {
  const percent = currentContext
    ? Math.round((currentContext.tokens / currentContext.window) * 100)
    : null;
  const cost = sessionUsage?.cost ? formatCost(sessionUsage.cost.total) : null;
  if (percent === null && !cost) return null;

  // Warn shortly before auto-compact, or near the window when it is off.
  const warn = percent !== null && percent >= Math.round((compactThreshold || 1) * 90);
  const trigger = (
    <>
      {percent !== null && (
        <span className={warn ? "text-destructive/70" : undefined}>{percent}%</span>
      )}
      {cost && (
        <span className={percent !== null ? "max-md:hidden" : undefined}>
          {percent !== null ? ` · ${cost}` : cost}
        </span>
      )}
    </>
  );

  const summary: { label: string; value: string }[] = [];
  if (currentContext) {
    summary.push({
      label: "Context",
      value: `${currentContext.tokens.toLocaleString()} / ${currentContext.window.toLocaleString()}`,
    });
  }
  // Share of all input served from cache; only when the provider reports caching.
  const cacheRead = sessionUsage?.cache_read_tokens ?? 0;
  const cacheWrite = sessionUsage?.cache_write_tokens ?? 0;
  if (sessionUsage?.input_tokens && (cacheRead || cacheWrite)) {
    summary.push({
      label: "Cache hit",
      value: `${Math.round((cacheRead / sessionUsage.input_tokens) * 100)}%`,
    });
  }

  return (
    <span className="mr-2 shrink-0">
      {summary.length > 0 || hasUsageRows(sessionUsage) ? (
        <StatsPopover trigger={trigger} align="end">
          <UsageGrid summary={summary} usage={sessionUsage} />
        </StatsPopover>
      ) : (
        <StatsText>{trigger}</StatsText>
      )}
    </span>
  );
}
