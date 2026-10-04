/**
 * Finished background commands that opened a turn, rendered in the user
 * bubble's place: the event that woke the agent, not the user and not the
 * reply's work. One row per command in ToolCard's row vocabulary, led by the
 * event rather than the tool name so it does not read as a second `bash`
 * call; the call that started the command sits in an earlier turn's work.
 */

import { Terminal } from "lucide-react";
import { useState } from "react";
import type { JobResult } from "../../types";
import { cn } from "../../utils/cn";
import { BashBody, StatusPill } from "./ToolCard";

export function JobMarker({ jobs }: { jobs: JobResult[] }) {
  return (
    <div className="flex flex-col gap-3 px-5 max-md:px-4">
      {jobs.map((job) => (
        <JobRow key={job.tool_use_id} job={job} />
      ))}
    </div>
  );
}

function JobRow({ job }: { job: JobResult }) {
  const [expanded, setExpanded] = useState(false);
  const failed = job.exit_code !== 0;

  return (
    <div className="group/tool">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <Terminal
          className="size-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="text-[13px] shrink-0 tracking-tight text-foreground/90 transition-colors duration-200 group-hover/tool:text-foreground">
          Background finished
        </span>
        <span className="min-w-0 text-[13px] font-mono text-muted-foreground/60 truncate">
          {job.label}
        </span>
        <StatusPill tone={failed ? "error" : "muted"}>
          exit {job.exit_code}
        </StatusPill>
      </button>

      <div
        data-expanded={expanded}
        className={cn(
          "chat-collapsible-body grid transition-[grid-template-rows,opacity] duration-300 ease-in-out",
          expanded
            ? "grid-rows-[1fr] opacity-100"
            : "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="overflow-hidden">
          <div className="mt-2 ml-5">
            {/* The row already names the command; the body is the output alone. */}
            <BashBody args={{}} display={job.output} />
          </div>
        </div>
      </div>
    </div>
  );
}
