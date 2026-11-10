/**
 * Finished background jobs that opened a turn, rendered in the user bubble's
 * place: the event that woke the agent, not the user and not the reply's
 * work. One row per job in ToolCard's row vocabulary, led by the event
 * rather than the tool name so it does not read as a second call; the call
 * that started the job sits in an earlier turn's work.
 */

import { Bot, Terminal } from "lucide-react";

import { MarkdownBlock } from "@/components/markdown/MarkdownBlock";
import { formatAgentOutcome } from "@/components/messages/toolArgs";
import { BashSurface, StatusPill, ToolRow, TranscriptAction } from "@/components/messages/ToolCard";
import type { AgentStatus, JobResult } from "@/types";

const AGENT_ENDED: Record<AgentStatus, string> = {
  completed: "Agent finished",
  failed: "Agent failed",
  cancelled: "Agent stopped",
};

export function JobMarker({ jobs }: { jobs: JobResult[] }) {
  return (
    <div className="flex flex-col gap-3 px-5 max-md:px-4">
      {jobs.map((job) =>
        job.name === "agent" ? (
          <ToolRow
            key={job.tool_use_id}
            icon={Bot}
            label={AGENT_ENDED[job.status]}
            preview={job.label}
            prosePreview
            suffix={
              <StatusPill tone={job.status === "failed" ? "error" : "muted"}>
                {formatAgentOutcome(job)}
              </StatusPill>
            }
            action={<TranscriptAction childId={job.session_id} />}
          >
            <div className="text-sm leading-relaxed">
              <MarkdownBlock content={job.output} />
            </div>
          </ToolRow>
        ) : (
          <ToolRow
            key={job.tool_use_id}
            icon={Terminal}
            label="Background finished"
            preview={job.command}
            suffix={
              <StatusPill tone={job.exit_code !== 0 ? "error" : "muted"}>
                exit {job.exit_code}
              </StatusPill>
            }
          >
            <BashSurface command={job.command} output={job.output} followTail />
          </ToolRow>
        ),
      )}
    </div>
  );
}
