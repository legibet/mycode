/**
 * What the session has in flight besides the composer: background commands,
 * then the messages queued for the next turn. One card behind the composer,
 * showing above its top edge.
 */

import {
  ArrowUpToLine,
  CornerDownRight,
  Paperclip,
  Pencil,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";

import type { BackgroundJobInfo } from "@/api";
import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";
import type { PendingInput } from "@/types";
import { formatDuration } from "@/utils/format";

interface InFlightCardProps {
  jobs: BackgroundJobInfo[];
  onStopJob: (toolUseId: string) => void;
  queued: PendingInput[];
  onSteerQueued: (id: string) => void;
  onEditQueued: (id: string) => void;
  onRemoveQueued: (id: string) => void;
}

export function InFlightCard({
  jobs,
  onStopJob,
  queued,
  onSteerQueued,
  onEditQueued,
  onRemoveQueued,
}: InFlightCardProps) {
  if (jobs.length === 0 && queued.length === 0) return null;

  return (
    <div className="mx-4 -mb-3 divide-y divide-border/40 rounded-t-lg bg-card pb-3 shadow-hairline">
      {jobs.length > 0 && <BackgroundJobs jobs={jobs} onStop={onStopJob} />}
      {queued.length > 0 && (
        <ul aria-label="Queued messages" className="divide-y divide-border/40">
          {queued.map((item) => {
            const attachmentCount = item.attachments.length + item.submission.workspaceFiles.length;
            return (
              <li key={item.id} className="flex items-center gap-2.5 px-3.5 py-2 text-sm leading-5">
                <CornerDownRight
                  aria-hidden="true"
                  className="size-3.5 shrink-0 text-muted-foreground/70"
                />
                <span
                  className="min-w-0 flex-1 truncate text-foreground"
                  title={item.submission.text}
                >
                  {item.submission.text.split("\n", 1)[0]}
                </span>
                {attachmentCount > 0 && (
                  <span
                    className="flex shrink-0 items-center gap-0.5 text-xs text-muted-foreground"
                    title={`${attachmentCount} attachment${attachmentCount === 1 ? "" : "s"}`}
                  >
                    <Paperclip className="size-3" />
                    {attachmentCount}
                  </span>
                )}
                <span className="flex shrink-0 items-center gap-0.5">
                  <Button
                    variant="ghost"
                    size="xs"
                    title="Steer the current turn"
                    onClick={() => onSteerQueued(item.id)}
                  >
                    <ArrowUpToLine data-icon="inline-start" className="size-3.5" />
                    Steer
                  </Button>
                  {!item.partial && (
                    <SimpleTooltip label="Edit">
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Edit"
                        onClick={() => onEditQueued(item.id)}
                      >
                        <Pencil className="size-3.5" />
                      </Button>
                    </SimpleTooltip>
                  )}
                  <SimpleTooltip label="Remove">
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      aria-label="Remove"
                      onClick={() => onRemoveQueued(item.id)}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </SimpleTooltip>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Mounted only while a command runs, so its clock starts fresh each time. */
function BackgroundJobs({
  jobs,
  onStop,
}: {
  jobs: BackgroundJobInfo[];
  onStop: (toolUseId: string) => void;
}) {
  // Running times tick once a second.
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="pt-2">
      <div className="flex items-center gap-1.5 px-3.5 pb-0.5 text-2xs leading-4 text-muted-foreground">
        <Terminal aria-hidden="true" className="size-3" />
        Background
        <span className="text-muted-foreground/60 tabular-nums">{jobs.length}</span>
      </div>
      <ul aria-label="Background commands">
        {jobs.map((job) => (
          <li key={job.tool_use_id} className="flex items-center gap-2.5 px-3.5 py-1">
            <span
              className="min-w-0 flex-1 truncate font-mono text-xs text-foreground/75"
              title={job.label}
            >
              {job.label}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
              {formatDuration(Math.max(0, now - Date.parse(job.started_at)))}
            </span>
            <Button
              variant="ghost"
              size="xs"
              title="Stop the command"
              onClick={() => onStop(job.tool_use_id)}
            >
              <Square data-icon="inline-start" className="fill-current" />
              Stop
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
