/**
 * Tool execution display.
 * Zero container. Two-tier typography: tool name (sans medium, foreground)
 * vs everything else (mono regular, muted-foreground). Click the row text
 * to toggle the per-tool body (bash / read / write / edit / generic); an
 * agent's row also opens its transcript from an action.
 */

import { Bot, type LucideIcon, Maximize2, Terminal } from "lucide-react";
import {
  memo,
  type ReactNode,
  type UIEvent,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { EditDiff } from "@/components/markdown/EditDiff";
import { MarkdownBlock } from "@/components/markdown/MarkdownBlock";
import { LiveLogText, useLiveLog } from "@/components/messages/liveLog";
import { useSessionLinks } from "@/components/messages/sessionLinks";
import {
  asNumber,
  asString,
  formatAgentOutcome,
  getAgentReply,
  getEditHunks,
  getEditStats,
  getJobOutput,
  getPreview,
  getReadHint,
  getWriteHint,
  type ToolArgs,
  type ToolMeta,
} from "@/components/messages/toolArgs";
import { TOOL_ICON } from "@/components/messages/toolIcons";
import { Button } from "@/components/ui/button";
import { Collapse } from "@/components/ui/collapse";
import { SimpleTooltip } from "@/components/ui/tooltip";
import { cn } from "@/utils/cn";
import { readAgentOutcome } from "@/utils/messages";

interface ToolCardProps {
  /** The `tool_use` id. */
  id: string;
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

// Within this distance of the bottom, a followed body keeps to the tail.
const TAIL_THRESHOLD = 48;

export function ToolSurface({
  head,
  body,
  followTail = false,
  fixed = false,
}: {
  head?: ReactNode;
  body?: ReactNode;
  /** Keep the body scrolled to its end as it grows, until the reader scrolls up. */
  followTail?: boolean | undefined;
  /** Keep one size whatever the output: the head at three lines, the body at a fixed height. */
  fixed?: boolean | undefined;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);

  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (followTail && el && followingRef.current) el.scrollTop = el.scrollHeight;
  });

  const handleScroll = (event: UIEvent<HTMLDivElement>) => {
    const el = event.currentTarget;
    followingRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < TAIL_THRESHOLD;
  };

  if (!head && !body) return null;
  return (
    <div
      className={cn(
        "overflow-hidden rounded-md bg-code font-mono text-ui shadow-hairline",
        // A fixed surface gives way to a short viewport: the body shrinks first.
        fixed && "flex min-h-0 flex-col",
      )}
    >
      {head && (
        <div
          className={cn(
            "scrollbar-subtle overflow-auto px-3 py-2 leading-normal",
            fixed ? "max-h-[calc(3lh+1rem)] shrink-0" : "max-h-60",
          )}
        >
          {head}
        </div>
      )}
      {body && (
        <div
          ref={bodyRef}
          onScroll={followTail ? handleScroll : undefined}
          className={cn(
            "scrollbar-subtle overflow-auto px-3 py-2 leading-relaxed whitespace-pre-wrap text-muted-foreground",
            fixed ? "h-64 min-h-0" : "max-h-60",
          )}
        >
          {body}
        </div>
      )}
    </div>
  );
}

/** A bash command over its output: the card's body, the marker's, and the jobs panel's. */
export function BashSurface({
  command,
  output,
  followTail,
  fixed,
}: {
  command: string;
  output: ReactNode;
  followTail?: boolean | undefined;
  fixed?: boolean | undefined;
}) {
  return (
    <ToolSurface
      head={
        command && (
          <>
            <span className="text-muted-foreground/40 select-none">$ </span>
            <span className="wrap-anywhere whitespace-pre-wrap text-foreground">{command}</span>
          </>
        )
      }
      body={output}
      followTail={followTail}
      fixed={fixed}
    />
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

const ROW_CLASS = "flex min-w-0 items-center gap-1.5";

interface RowContentProps {
  icon: LucideIcon;
  label: string;
  status?: "pending" | "error" | undefined;
  preview: string;
  /** The preview is prose, such as an agent's description, not code. */
  prosePreview?: boolean;
  suffix?: ReactNode;
}

function RowContent({
  icon: Icon,
  label,
  status,
  preview,
  prosePreview = false,
  suffix,
}: RowContentProps) {
  return (
    <>
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
        <span
          className={cn(
            "min-w-0 truncate text-ui",
            prosePreview ? "text-muted-foreground" : "font-mono text-muted-foreground/60",
          )}
        >
          {preview}
        </span>
      )}

      {suffix}
    </>
  );
}

export function ToolRow({
  action,
  children,
  ...content
}: RowContentProps & {
  /** A control at the row's end, beside the toggle. */
  action?: ReactNode;
  /** The body the row opens to, or a function of whether it is open, for a
   * body that works only while seen. Without one the row does not toggle. */
  children?: ReactNode | ((expanded: boolean) => ReactNode);
}) {
  const [expanded, setExpanded] = useState(false);
  // An action sits right after the row's text, not at the far edge.
  const rowClass = cn(ROW_CLASS, !action && "flex-1");

  if (children === undefined) {
    return (
      <div className="flex items-center gap-2">
        <div className={rowClass}>
          <RowContent {...content} />
        </div>
        {action}
      </div>
    );
  }

  return (
    <div className="group/tool">
      <div className="flex items-center gap-2">
        <button
          type="button"
          className={cn(
            rowClass,
            "cursor-pointer rounded-sm text-left select-none focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset",
          )}
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          <RowContent {...content} />
        </button>
        {action}
      </div>

      <Collapse open={expanded}>
        {/* Plain indent: the body is already a bounded surface, so a rail
            beside it would just be a second parallel line. */}
        <div className="mt-2 ml-5">
          {typeof children === "function" ? children(expanded) : children}
        </div>
      </Collapse>
    </div>
  );
}

/** A row's icon action that opens a subagent's transcript; nothing outside a view with links. */
export function TranscriptAction({ childId }: { childId: string }) {
  const links = useSessionLinks();
  if (!links) return null;
  return (
    <SimpleTooltip label="Open transcript">
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label="Open transcript"
        onClick={() => links.openAgent(childId)}
        // Keeps the row at its text height.
        className="-my-1"
      >
        <Maximize2 />
      </Button>
    </SimpleTooltip>
  );
}

const AGENT_LIVE_LINES = 3;

function AgentCard({
  args,
  output,
  finalOutput,
  metadata,
  pending,
  isError,
}: Omit<ToolCardProps, "name">) {
  // The call names its child once the child is set up, while it runs and in its result.
  const childId = asString(metadata?.["session_id"]);
  const background = metadata?.["background"] === true || args?.["background"] === true;
  const steered = metadata?.["steered"] === true;
  // A background start carries its job's outcome once the job has finished.
  const outcome = readAgentOutcome(metadata);
  // A background start's own result is a notice; its reply arrives with its job.
  const replyFromJob = background && !steered && !isError;

  const pill = steered ? (
    <StatusPill>steered</StatusPill>
  ) : outcome ? (
    <StatusPill tone={outcome.status === "failed" ? "error" : "muted"}>
      {formatAgentOutcome(outcome)}
    </StatusPill>
  ) : pending && !background ? (
    <StatusPill>running</StatusPill>
  ) : isError ? (
    <StatusPill tone="error">failed</StatusPill>
  ) : background ? (
    <StatusPill pulse={metadata?.["running"] === true}>
      {metadata?.["running"] === true ? "background · running" : "background"}
    </StatusPill>
  ) : null;

  const liveLines =
    pending && !background
      ? (output ?? "").split("\n").filter(Boolean).slice(-AGENT_LIVE_LINES)
      : [];
  const reply = replyFromJob
    ? (getJobOutput(metadata) ?? "").trim()
    : !pending && !steered
      ? getAgentReply(finalOutput ?? "").trim()
      : "";

  // The body, like every tool card's: the latest progress lines while a
  // blocking call runs, the reply once it has one, the tool's output
  // otherwise. A background start has nothing to open until its job ends.
  const body =
    liveLines.length > 0 ? (
      liveLines.map((line, index) => (
        <div
          // Lines only append, so a position names one line.
          key={index}
          className="truncate font-mono text-xs leading-5 text-muted-foreground/70"
        >
          {line}
        </div>
      ))
    ) : reply ? (
      <MarkdownBlock content={reply} />
    ) : replyFromJob ? undefined : (
      <ToolSurface body={finalOutput ?? output} />
    );

  return (
    <ToolRow
      icon={Bot}
      label="agent"
      status={pending && !background ? "pending" : isError ? "error" : undefined}
      preview={getPreview("agent", args)}
      prosePreview
      suffix={pill}
      action={childId && <TranscriptAction childId={childId} />}
    >
      {body}
    </ToolRow>
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

export const ToolCard = memo(function ToolCard(props: ToolCardProps) {
  return props.name === "agent" ? <AgentCard {...props} /> : <ToolCallCard {...props} />;
});

function ToolCallCard({
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
      (expanded: boolean) => (
        <BashBody args={args} metadata={metadata} display={display} open={expanded} />
      )
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
}

/** A bash call's body: the command over its output. */
function BashBody({
  args,
  metadata,
  display,
  open,
}: {
  args: ToolArgs;
  metadata: ToolMeta;
  display: string;
  open: boolean;
}) {
  const command = asString(args?.["command"]);
  if (metadata?.["background"] !== true) return <BashSurface command={command} output={display} />;
  return (
    <BackgroundBashBody
      jobId={asString(metadata["job_id"])}
      command={command}
      jobOutput={getJobOutput(metadata)}
      open={open}
    />
  );
}

/**
 * A background start's own result is only a notice, so its output is the
 * job's: its live log until the job's output arrives, streamed only while the
 * row is open. One surface holds both, so the tail the reader follows stays
 * in view when the job ends.
 */
function BackgroundBashBody({
  jobId,
  command,
  jobOutput,
  open,
}: {
  jobId: string;
  command: string;
  jobOutput: string | null;
  open: boolean;
}) {
  const links = useSessionLinks();
  const log = useLiveLog(
    jobOutput === null && open && jobId && links?.logUrl ? links.logUrl(jobId) : null,
  );
  return (
    <BashSurface command={command} output={jobOutput ?? <LiveLogText log={log} />} followTail />
  );
}
