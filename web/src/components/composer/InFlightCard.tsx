/**
 * What the session has in flight besides the composer: background jobs
 * (commands and agents) as one bar, then the messages queued for the next
 * turn. One card behind the composer, showing above its top edge.
 */

import { ArrowUpToLine, CornerDownRight, Paperclip, Pencil, Trash2 } from "lucide-react";
import { useRef } from "react";

import type { BackgroundJobInfo } from "@/api";
import { JobsBar } from "@/components/composer/JobsBar";
import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";
import type { PendingInput, RenderMessage } from "@/types";

interface InFlightCardProps {
  jobs: BackgroundJobInfo[];
  onStopJob: (jobId: string) => void;
  /** The session's rendered messages, where the jobs' calls are. */
  messages: RenderMessage[];
  queued: PendingInput[];
  onSteerQueued: (id: string) => void;
  onEditQueued: (id: string) => void;
  onRemoveQueued: (id: string) => void;
}

export function InFlightCard({
  jobs,
  onStopJob,
  messages,
  queued,
  onSteerQueued,
  onEditQueued,
  onRemoveQueued,
}: InFlightCardProps) {
  const cardRef = useRef<HTMLDivElement>(null);

  return (
    // Hidden while empty: the jobs bar decides itself when it shows, since it
    // stays while its panel is open on a command that has finished.
    <div
      ref={cardRef}
      className="mx-4 -mb-3 divide-y divide-border/40 rounded-t-lg bg-card pb-3 shadow-hairline empty:hidden"
    >
      <JobsBar jobs={jobs} onStopJob={onStopJob} messages={messages} anchor={cardRef} />
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
