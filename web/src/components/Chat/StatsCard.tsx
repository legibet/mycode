/** Usage stats card: muted trigger text that opens a label/value card above
 * it — on hover for pointers, on tap for touch. */

import { Fragment, type ReactNode } from "react";
import type { UsageTotals } from "../../types";
import { cn } from "../../utils/cn";
import { formatCost } from "../../utils/format";
import { Popover, PopoverContent, PopoverTrigger } from "../ui/popover";

const MUTED_TEXT =
  "cursor-default text-xs tabular-nums text-muted-foreground/50";

export function StatsPopover({
  trigger,
  align = "start",
  children,
}: {
  trigger: ReactNode;
  align?: "start" | "end";
  children: ReactNode;
}) {
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={0}
        className={cn(
          MUTED_TEXT,
          "rounded-sm outline-none transition-colors duration-150 hover:text-muted-foreground/90 focus-visible:text-muted-foreground/90 data-popup-open:text-muted-foreground/90",
        )}
      >
        {trigger}
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align={align}
        sideOffset={4}
        className="w-max gap-0 rounded-lg px-3.5 py-3 text-xs"
      >
        {children}
      </PopoverContent>
    </Popover>
  );
}

/** Trigger text without a card, for stats that have nothing to break down. */
export function StatsText({ children }: { children: ReactNode }) {
  return <span className={MUTED_TEXT}>{children}</span>;
}

interface UsageRow {
  label: string;
  tokens: number;
  cost: number | undefined;
}

function usageRows(usage: UsageTotals | null | undefined): UsageRow[] {
  if (!usage) return [];
  const { cost } = usage;
  const rows: UsageRow[] = [];
  if (usage.input_tokens !== undefined) {
    rows.push({
      label: "Input",
      tokens:
        usage.input_tokens -
        (usage.cache_read_tokens ?? 0) -
        (usage.cache_write_tokens ?? 0),
      cost: cost?.input,
    });
  }
  if (usage.cache_read_tokens) {
    rows.push({
      label: "Cache read",
      tokens: usage.cache_read_tokens,
      cost: cost?.cache_read,
    });
  }
  if (usage.cache_write_tokens) {
    rows.push({
      label: "Cache write",
      tokens: usage.cache_write_tokens,
      cost: cost?.cache_write,
    });
  }
  if (usage.output_tokens !== undefined) {
    rows.push({
      label: "Output",
      tokens: usage.output_tokens,
      cost: cost ? (cost.output ?? 0) + (cost.reasoning ?? 0) : undefined,
    });
  }
  return rows;
}

function usageTotalTokens(
  usage: UsageTotals | null | undefined,
): number | undefined {
  if (usage?.total_tokens !== undefined) return usage.total_tokens;
  if (usage?.input_tokens !== undefined && usage.output_tokens !== undefined) {
    return usage.input_tokens + usage.output_tokens;
  }
  return undefined;
}

/** Whether `usage` has any token rows for a UsageGrid. */
export function hasUsageRows(usage: UsageTotals | null | undefined): boolean {
  return usageRows(usage).length > 0 || usageTotalTokens(usage) !== undefined;
}

/**
 * One grid for a stats card: optional summary rows (label + value spanning
 * the value columns), then the token table closed by a Total row. A known
 * cost adds a cost column: per row when it has a breakdown, always on Total.
 */
export function UsageGrid({
  summary = [],
  usage,
}: {
  summary?: { label: string; value: string }[];
  usage?: UsageTotals | null | undefined;
}) {
  const rows = usageRows(usage);
  const totalTokens = usageTotalTokens(usage);
  const cost = usage?.cost;
  const breakdown = cost?.input !== undefined && cost.output !== undefined;

  return (
    <span
      className={cn(
        "grid items-baseline gap-x-5 gap-y-1",
        cost
          ? "grid-cols-[max-content_max-content_max-content]"
          : "grid-cols-[max-content_max-content]",
      )}
    >
      {summary.map((row) => (
        <Fragment key={row.label}>
          <span className="text-muted-foreground">{row.label}</span>
          <span className="col-[2/-1] text-right tabular-nums">
            {row.value}
          </span>
        </Fragment>
      ))}
      {summary.length > 0 && rows.length > 0 ? (
        <span
          aria-hidden
          className="col-span-full mt-1 mb-0.5 border-t border-border/50"
        />
      ) : null}
      {rows.map((row) => (
        <Fragment key={row.label}>
          <span className="text-muted-foreground">{row.label}</span>
          <span className="text-right tabular-nums">
            {row.tokens.toLocaleString()}
          </span>
          {cost ? (
            <span className="text-right tabular-nums">
              {breakdown ? formatCost(row.cost ?? 0) : null}
            </span>
          ) : null}
        </Fragment>
      ))}
      {totalTokens !== undefined || cost ? (
        <span
          className={cn(
            "col-span-full grid grid-cols-subgrid items-baseline font-medium",
            rows.length > 0 && "mt-1 border-t border-border/50 pt-1.5",
          )}
        >
          <span>Total</span>
          <span className="text-right tabular-nums">
            {totalTokens?.toLocaleString()}
          </span>
          {cost ? (
            <span className="text-right tabular-nums">
              {formatCost(cost.total)}
            </span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}
