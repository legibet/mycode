/**
 * The session's running background jobs as one bar on the in-flight card:
 * which kinds run and the longest running time. It opens a panel above the
 * card that lists each job with Stop; a command opens in place to its live
 * output, an agent to its transcript.
 */

import { ArrowLeft, ChevronUp, Square, Terminal } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";

import type { BackgroundJobInfo } from "@/api";
import { LiveLogText, useLiveLog } from "@/components/messages/liveLog";
import { useSessionLinks } from "@/components/messages/sessionLinks";
import { asNumber, asString } from "@/components/messages/toolArgs";
import { BashSurface, StatusPill } from "@/components/messages/ToolCard";
import { TOOL_ICON } from "@/components/messages/toolIcons";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { RenderMessage, ToolUseBlock } from "@/types";
import { cn } from "@/utils/cn";
import { formatDuration } from "@/utils/format";
import { findToolUse } from "@/utils/messages";

interface JobsBarProps {
  jobs: BackgroundJobInfo[];
  onStopJob: (jobId: string) => void;
  /** The session's rendered messages, where the calls that started the jobs are. */
  messages: RenderMessage[];
  /** What the panel lines up with: the in-flight card. */
  anchor: RefObject<HTMLElement | null>;
}

/** The list, with the row to focus on return from a command; or one command. */
type PanelView =
  | { kind: "list"; focus: string | null }
  | { kind: "command"; job: BackgroundJobInfo };

const KINDS = [
  { name: "bash", noun: "command" },
  { name: "agent", noun: "agent" },
] as const;

export function JobsBar({ jobs, onStopJob, messages, anchor }: JobsBarProps) {
  const links = useSessionLinks();
  const [open, setOpen] = useState(false);
  // The bar outlives its last job while the panel is open or closing, so a
  // finished command stays on screen until the reader leaves it.
  const [panelShown, setPanelShown] = useState(false);
  const [view, setView] = useState<PanelView>({ kind: "list", focus: null });
  const visible = jobs.length > 0 || panelShown;

  // Running times tick once a second.
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!visible) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [visible]);

  // A list with nothing left in it closes.
  if (open && view.kind === "list" && jobs.length === 0) setOpen(false);

  if (!visible) return null;

  const elapsed = (job: BackgroundJobInfo) => Math.max(0, now - Date.parse(job.started_at));
  const kinds = KINDS.map((kind) => ({
    ...kind,
    count: jobs.filter((job) => job.name === kind.name).length,
  })).filter((kind) => kind.count > 0);
  const summary = kinds
    .map((kind) => `${kind.count} ${kind.noun}${kind.count === 1 ? "" : "s"}`)
    .join(", ");
  const showList = () =>
    setView((current) => ({
      kind: "list",
      focus: current.kind === "command" ? current.job.id : null,
    }));

  return (
    <Popover
      open={open}
      onOpenChange={(next, details) => {
        if (next) {
          // A lone command has nothing to choose from: open straight to it.
          const only = jobs.length === 1 ? jobs[0] : undefined;
          setView(
            only?.name === "bash" ? { kind: "command", job: only } : { kind: "list", focus: null },
          );
          setPanelShown(true);
          setOpen(true);
        } else if (details.reason === "escape-key" && view.kind === "command") {
          // Esc steps back to the list before it closes the panel.
          details.cancel();
          showList();
        } else {
          setOpen(false);
        }
      }}
      onOpenChangeComplete={(next) => {
        if (!next) setPanelShown(false);
      }}
    >
      <PopoverTrigger
        aria-label={summary ? `Background: ${summary}` : "Background"}
        className="group/bar flex w-full cursor-pointer items-center gap-2.5 rounded-t-lg px-3.5 py-2 text-xs leading-5 text-muted-foreground transition-colors outline-none hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-inset data-popup-open:text-foreground"
      >
        <span>Background</span>
        {kinds.map(({ name, count }) => {
          const Icon = TOOL_ICON[name] ?? Terminal;
          return (
            <span key={name} className="flex items-center gap-1 tabular-nums">
              <Icon aria-hidden="true" className="size-3" />
              {count}
            </span>
          );
        })}
        <span className="ml-auto tabular-nums">
          {jobs.length > 0 ? formatDuration(Math.max(...jobs.map(elapsed))) : "done"}
        </span>
        <ChevronUp aria-hidden="true" className="size-3.5 group-data-popup-open/bar:rotate-180" />
      </PopoverTrigger>
      <PopoverContent
        anchor={anchor}
        side="top"
        sideOffset={6}
        className="max-h-(--available-height) w-(--anchor-width) gap-0 rounded-lg p-1.5"
      >
        {view.kind === "command" ? (
          <CommandView
            key={view.job.id}
            job={view.job}
            running={jobs.some((job) => job.id === view.job.id)}
            messages={messages}
            elapsed={elapsed}
            logUrl={links?.logUrl}
            onBack={showList}
            onStop={onStopJob}
          />
        ) : (
          <JobList
            jobs={jobs}
            focus={view.focus}
            messages={messages}
            elapsed={elapsed}
            onOpen={(job) => {
              if (job.name === "bash") {
                setView({ kind: "command", job });
              } else if (job.session_id) {
                links?.openAgent(job.session_id);
                setOpen(false);
              }
            }}
            onStop={onStopJob}
          />
        )}
      </PopoverContent>
    </Popover>
  );
}

function StopButton({
  job,
  onStop,
  className,
}: {
  job: BackgroundJobInfo;
  onStop: (id: string) => void;
  className?: string;
}) {
  return (
    <Button
      variant="ghost"
      size="xs"
      className={className}
      title={job.name === "agent" ? "Stop the agent" : "Stop the command"}
      onClick={() => onStop(job.id)}
    >
      <Square data-icon="inline-start" className="fill-current" />
      Stop
    </Button>
  );
}

/** The full command from the call that started a bash job; its job's shortened label until the call is found. */
function commandOf(call: ToolUseBlock | null, label = ""): string {
  return asString(call?.input["command"]) || label;
}

function JobList({
  jobs,
  focus,
  messages,
  elapsed,
  onOpen,
  onStop,
}: {
  jobs: BackgroundJobInfo[];
  /** The row to focus, the command the reader came back from. */
  focus: string | null;
  messages: RenderMessage[];
  elapsed: (job: BackgroundJobInfo) => number;
  onOpen: (job: BackgroundJobInfo) => void;
  onStop: (jobId: string) => void;
}) {
  const listRef = useRef<HTMLUListElement>(null);
  useEffect(() => {
    if (!focus) return;
    listRef.current?.querySelector<HTMLElement>(`[data-job="${CSS.escape(focus)}"]`)?.focus();
  }, [focus]);

  return (
    <ul
      ref={listRef}
      aria-label="Background jobs"
      className="scrollbar-subtle max-h-[50dvh] overflow-y-auto"
    >
      {jobs.map((job) => {
        const Icon = TOOL_ICON[job.name] ?? Terminal;
        const bash = job.name === "bash";
        return (
          <li key={job.id} className="flex items-start gap-1">
            <button
              type="button"
              data-job={job.id}
              className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors outline-none hover:bg-accent focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-inset"
              onClick={() => onOpen(job)}
            >
              <Icon aria-hidden="true" className="mt-1 size-3 shrink-0 text-muted-foreground" />
              <span
                className={cn(
                  "line-clamp-2 min-w-0 flex-1 text-xs leading-5 wrap-anywhere text-foreground/85",
                  bash && "font-mono",
                )}
              >
                {bash ? commandOf(findToolUse(messages, job.tool_use_id), job.label) : job.label}
              </span>
              <span className="shrink-0 text-xs leading-5 text-muted-foreground tabular-nums">
                {formatDuration(elapsed(job))}
              </span>
            </button>
            {/* Level with the first line of a label that wraps. */}
            <StopButton job={job} onStop={onStop} className="mt-1" />
          </li>
        );
      })}
    </ul>
  );
}

function CommandView({
  job,
  running,
  messages,
  elapsed,
  logUrl,
  onBack,
  onStop,
}: {
  job: BackgroundJobInfo;
  /** Whether the job is still in the list; it leaves once it has finished. */
  running: boolean;
  messages: RenderMessage[];
  elapsed: (job: BackgroundJobInfo) => number;
  logUrl: ((jobId: string) => string) | undefined;
  onBack: () => void;
  onStop: (jobId: string) => void;
}) {
  const call = findToolUse(messages, job.tool_use_id);
  const command = commandOf(call, job.label);
  // The projection adds the exit code once the job's notification arrives.
  const exitCode = asNumber(call?.runtime?.metadata?.["exit_code"]);
  const log = useLiveLog(logUrl ? logUrl(job.id) : null);

  const backRef = useRef<HTMLButtonElement>(null);
  useEffect(() => backRef.current?.focus(), []);

  return (
    <div className="flex min-h-0 flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <Button ref={backRef} variant="ghost" size="icon-xs" aria-label="Back" onClick={onBack}>
          <ArrowLeft />
        </Button>
        <span className="min-w-0 flex-1">
          {running ? (
            <StatusPill>running · {formatDuration(elapsed(job))}</StatusPill>
          ) : exitCode !== null ? (
            <StatusPill tone={exitCode !== 0 ? "error" : "muted"}>exit {exitCode}</StatusPill>
          ) : (
            <StatusPill>finished</StatusPill>
          )}
        </span>
        {running && <StopButton job={job} onStop={onStop} />}
      </div>
      <BashSurface command={command} output={<LiveLogText log={log} />} followTail fixed />
    </div>
  );
}
