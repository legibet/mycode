/**
 * Finished background commands that opened a turn, rendered in the user
 * bubble's place: the event that woke the agent, not the user and not the
 * reply's work. One row per command in ToolCard's row vocabulary, led by the
 * event rather than the tool name so it does not read as a second `bash`
 * call; the call that started the command sits in an earlier turn's work.
 */

import { Terminal } from "lucide-react";

import { StatusPill, ToolRow, ToolSurface } from "@/components/messages/ToolCard";
import type { JobResult } from "@/types";

export function JobMarker({ jobs }: { jobs: JobResult[] }) {
  return (
    <div className="flex flex-col gap-3 px-5 max-md:px-4">
      {jobs.map((job) => (
        <ToolRow
          key={job.tool_use_id}
          icon={Terminal}
          label="Background finished"
          preview={job.label}
          suffix={
            <StatusPill tone={job.exit_code !== 0 ? "error" : "muted"}>
              exit {job.exit_code}
            </StatusPill>
          }
        >
          {/* The row already names the command; the body is the output alone. */}
          <ToolSurface body={job.output} />
        </ToolRow>
      ))}
    </div>
  );
}
