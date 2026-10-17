/**
 * Tool execution display.
 * Zero container. Two-tier typography: tool name (sans medium, foreground)
 * vs everything else (mono regular, muted-foreground). Click the row text
 * to toggle. Per-tool bodies (bash / read / write / edit / generic) are
 * preserved.
 */

import { type LucideIcon, Terminal } from "lucide-react";
import { memo, type ReactNode, useMemo, useState } from "react";

import { EditDiff } from "@/components/markdown/EditDiff";
import {
  asNumber,
  asString,
  getEditHunks,
  getEditStats,
  getPreview,
  getReadHint,
  getWriteHint,
  type ToolArgs,
  type ToolMeta,
} from "@/components/messages/toolArgs";
import { TOOL_ICON } from "@/components/messages/toolIcons";
import { Collapse } from "@/components/ui/collapse";
import { cn } from "@/utils/cn";

interface ToolCardProps {
  name: string;
  args?: Record<string, unknown>;
  output?: string | null | undefined;
  finalOutput?: string | null | undefined;
  metadata?: Record<string, unknown> | null | undefined;
  pending?: boolean | undefined;
  isError?: boolean | undefined;
}

// ---------------------------------------------------------------------------
// Shared code surface for text-based tools — one execution reads as one block:
// one fill, no internal rule. Input and output are told apart by text colour,
// the way a terminal does it; a rule has to out-shout the fill to be seen, and
// a second fill turns one surface into a titlebar plus a content pane.
// Diffs get their own surface (EditDiff.tsx): tinted rows run edge to edge.
// ---------------------------------------------------------------------------

export function ToolSurface({ head, body }: { head?: ReactNode; body?: ReactNode }) {
  if (!head && !body) return null;
  return (
    <div className="overflow-hidden rounded-md bg-code font-mono text-ui shadow-hairline">
      {head && (
        <div className="scrollbar-subtle max-h-60 overflow-auto px-3 py-2 leading-normal">
          {head}
        </div>
      )}
      {body && (
        <div className="scrollbar-subtle max-h-60 overflow-auto px-3 py-2 leading-relaxed whitespace-pre-wrap text-muted-foreground">
          {body}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The row: icon, label, mono preview, suffix; it toggles the body below it.
// JobMarker builds its rows from the same parts.
// ---------------------------------------------------------------------------

/**
 * A status beside a mono preview. Sans type on a muted fill keeps it apart
 * from the command text, which the muted mono suffixes blend into.
 */
export function StatusPill({
  tone = "muted",
  pulse = false,
  children,
}: {
  tone?: "muted" | "error";
  pulse?: boolean;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "shrink-0 rounded px-1.5 py-px text-2xs leading-4 tabular-nums",
        tone === "error" ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground",
        pulse && "animate-thinking",
      )}
    >
      {children}
    </span>
  );
}

export function ToolRow({
  icon: Icon,
  label,
  status,
  preview,
  suffix,
  children,
}: {
  icon: LucideIcon;
  label: string;
  status?: "pending" | "error" | undefined;
  preview: string;
  suffix?: ReactNode;
  /** The body the row opens to. */
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="group/tool">
      <button
        type="button"
        className="flex w-full cursor-pointer items-center gap-1.5 rounded-sm text-left select-none focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />

        <span
          className={cn(
            "shrink-0 text-ui tracking-tight transition-colors duration-200",
            status === "error"
              ? "text-destructive/90 group-hover/tool:text-destructive"
              : "text-foreground/90 group-hover/tool:text-foreground",
            status === "pending" && "animate-thinking",
          )}
        >
          {label}
        </span>

        {preview && (
          <span className="min-w-0 truncate font-mono text-ui text-muted-foreground/60">
            {preview}
          </span>
        )}

        {suffix}
      </button>

      <Collapse open={expanded}>
        {/* Plain indent: the body is already a bounded surface, so a rail
            beside it would just be a second parallel line. */}
        <div className="mt-2 ml-5">{children}</div>
      </Collapse>
    </div>
  );
}

function CollapsedSuffix({
  name,
  args,
  metadata,
}: {
  name: string;
  args: ToolArgs;
  metadata: ToolMeta;
}) {
  if (name === "edit") {
    const stats = getEditStats(metadata);
    if (!stats || (stats.added === 0 && stats.removed === 0)) return null;
    return (
      <span className="shrink-0 font-mono text-xs tabular-nums">
        <span className="text-diff-added">+{stats.added}</span>
        <span className="ml-1 text-diff-removed">−{stats.removed}</span>
      </span>
    );
  }

  if (name === "bash") {
    if (metadata?.["background"] !== true) return null;
    // The started job's state, added by the projection: running, or its exit code.
    const running = metadata["running"] === true;
    const exitCode = asNumber(metadata["exit_code"]);
    return (
      <StatusPill tone={exitCode ? "error" : "muted"} pulse={running}>
        {exitCode != null
          ? `background · exit ${exitCode}`
          : running
            ? "background · running"
            : "background"}
      </StatusPill>
    );
  }

  if (name === "websearch") {
    const results = asNumber(metadata?.["results"]);
    if (results == null) return null;
    return (
      <span className="shrink-0 font-mono text-xs text-muted-foreground/60 tabular-nums">
        {results} results
      </span>
    );
  }

  const hint = name === "read" ? getReadHint(args) : name === "write" ? getWriteHint(args) : "";
  if (!hint) return null;
  return (
    <span className="shrink-0 font-mono text-xs text-muted-foreground/60 tabular-nums">{hint}</span>
  );
}

// ---------------------------------------------------------------------------
// Expanded body components — one per tool type
// ---------------------------------------------------------------------------

function BashBody({ args, display }: { args: ToolArgs; display: string }) {
  const command = asString(args?.["command"]);

  return (
    <ToolSurface
      head={
        command && (
          <>
            <span className="text-muted-foreground/40 select-none">$ </span>
            <span className="break-all whitespace-pre-wrap text-foreground">{command}</span>
          </>
        )
      }
      body={display}
    />
  );
}

function WriteBody({
  args,
  display,
  isError,
}: {
  args: ToolArgs;
  display: string;
  isError: boolean;
}) {
  const content = asString(args?.["content"]);

  return (
    <ToolSurface
      head={content && <span className="whitespace-pre-wrap text-foreground/75">{content}</span>}
      body={isError ? display : null}
    />
  );
}

function EditBody({
  args,
  metadata,
  display,
  isError,
}: {
  args: ToolArgs;
  metadata: ToolMeta;
  display: string;
  isError: boolean;
}) {
  const hunks = useMemo(() => getEditHunks(args, metadata), [args, metadata]);

  if (hunks.length) {
    return (
      <div className="flex flex-col gap-2">
        <EditDiff path={asString(args?.["path"])} hunks={hunks} />
        {isError && <ToolSurface body={display} />}
      </div>
    );
  }

  return <GenericBody args={args} display={display} />;
}

function GenericBody({ args, display }: { args: ToolArgs; display: string }) {
  return (
    <ToolSurface
      head={
        args &&
        Object.keys(args).length > 0 &&
        Object.entries(args).map(([key, value]) => (
          <div key={key}>
            <span className="text-primary/80">{key}: </span>
            <span className="break-all whitespace-pre-wrap text-foreground/75">
              {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
            </span>
          </div>
        ))
      }
      body={display}
    />
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export const ToolCard = memo(function ToolCard({
  name,
  args,
  output,
  finalOutput,
  metadata,
  pending,
  isError,
}: ToolCardProps) {
  const display =
    typeof finalOutput === "string" ? finalOutput : typeof output === "string" ? output : "";
  const resolvedIsError =
    Boolean(isError) || (typeof finalOutput === "string" && finalOutput.startsWith("error:"));

  const body =
    name === "bash" ? (
      <BashBody args={args} display={display} />
    ) : name === "read" || name === "webfetch" || name === "websearch" ? (
      <ToolSurface body={display} />
    ) : name === "write" ? (
      <WriteBody args={args} display={display} isError={resolvedIsError} />
    ) : name === "edit" ? (
      <EditBody args={args} metadata={metadata} display={display} isError={resolvedIsError} />
    ) : (
      <GenericBody args={args} display={display} />
    );

  return (
    <ToolRow
      icon={TOOL_ICON[name] ?? Terminal}
      label={name}
      status={pending ? "pending" : resolvedIsError ? "error" : undefined}
      preview={getPreview(name, args)}
      suffix={<CollapsedSuffix name={name} args={args} metadata={metadata} />}
    >
      {body}
    </ToolRow>
  );
});
