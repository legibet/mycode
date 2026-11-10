/**
 * A subagent's read-only transcript over the chat. A centered dialog on
 * desktop, a full-screen sheet on mobile; App owns which child it shows and
 * closes it with the session.
 */

import { Bot, Square, X } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import type { BackgroundJobInfo } from "@/api";
import { PermissionPrompt } from "@/components/composer/PermissionPrompt";
import { MessageList } from "@/components/messages/MessageList";
import { type SessionLinks, SessionLinksContext } from "@/components/messages/sessionLinks";
import { asString, formatAgentOutcome } from "@/components/messages/toolArgs";
import { StatusPill } from "@/components/messages/ToolCard";
import { Button } from "@/components/ui/button";
import { ResponsiveDialog } from "@/components/ui/responsive-dialog";
import { useAgentSession } from "@/hooks/useAgentSession";
import type { SubscribeServerEvents } from "@/hooks/useChat";
import type { PermissionRequest, RenderMessage, ToolUseBlock, TranscriptTarget } from "@/types";
import { isChatMessage } from "@/types";
import { formatDuration } from "@/utils/format";

/** What the transcript reads from the parent session, and what it can ask of it. */
interface ViewProps {
  /** The parent session's rendered messages, where the calls that started children live. */
  messages: RenderMessage[];
  /** The parent session's running background jobs. */
  jobs: BackgroundJobInfo[];
  onOpen: (transcript: TranscriptTarget) => void;
  onStopJob: (toolUseId: string) => void;
  /** The app's `/api/events` subscription, which announces the child's runs. */
  subscribeEvents: SubscribeServerEvents;
}

interface TranscriptDialogProps extends ViewProps {
  /** The child it shows; null closes it. */
  transcript: TranscriptTarget | null;
  onClose: () => void;
  /** The parent session's next permission request. The dialog covers the chat's
   * prompt, so the request is answered here while it is open. */
  permission: PermissionRequest | null;
  onDecidePermission: (decision: "allow" | "deny") => void;
}

export function TranscriptDialog({
  transcript,
  onClose,
  permission,
  onDecidePermission,
  ...view
}: TranscriptDialogProps) {
  // The popup stays mounted through its exit animation; the last child fills
  // it until then, so it does not fade out empty.
  const [shown, setShown] = useState(transcript);
  if (transcript && transcript !== shown) setShown(transcript);

  // The dialog opens on Close: the first control in the header may be Stop.
  const closeRef = useRef<HTMLButtonElement>(null);
  const close = (
    <Button
      ref={closeRef}
      variant="ghost"
      size="icon"
      aria-label="Close"
      onClick={onClose}
      className="-mr-1"
    >
      <X className="size-3.5" />
    </Button>
  );

  return (
    <ResponsiveDialog
      open={transcript !== null}
      onClose={onClose}
      title="Agent transcript"
      initialFocus={closeRef}
      fullScreen
      className="h-[min(90dvh,56rem)] w-[min(56rem,calc(100vw-4rem))] overflow-hidden"
    >
      {shown && (
        // Keyed by the child, so a different one loads its own session.
        <AgentView
          key={shown.childId}
          parentId={shown.sessionId}
          childId={shown.childId}
          close={close}
          {...view}
        />
      )}
      {/* Not while it closes: the composer shows the prompt again from then on. */}
      {transcript && permission && (
        <div className="shrink-0 pb-3 max-md:pb-[calc(0.75rem+env(safe-area-inset-bottom))]">
          <PermissionPrompt request={permission} onDecide={onDecidePermission} />
        </div>
      )}
    </ResponsiveDialog>
  );
}

/** A quiet line in the middle of an empty body. */
function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-1 items-center justify-center p-8 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}

/** Time since `since`, ticking once a second while mounted. */
function Elapsed({ since }: { since: string }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return <>{formatDuration(Math.max(0, now - Date.parse(since)))}</>;
}

/** The parent's `agent` calls that named this child, its start first, then its resumes. */
function callsFor(messages: RenderMessage[], childId: string): ToolUseBlock[] {
  return messages.flatMap((message) =>
    isChatMessage(message)
      ? message.content.filter(
          (block): block is ToolUseBlock =>
            block.type === "tool_use" &&
            block.name === "agent" &&
            block.runtime?.metadata?.["session_id"] === childId,
        )
      : [],
  );
}

function AgentView({
  parentId,
  childId,
  close,
  messages,
  jobs,
  onOpen,
  onStopJob,
  subscribeEvents,
}: ViewProps & { parentId: string; childId: string; close: ReactNode }) {
  const agent = useAgentSession(parentId, childId, subscribeEvents);
  const calls = callsFor(messages, childId);
  const job = jobs.find((item) => item.name === "agent" && item.session_id === childId);
  // The call that started the child names it until the child loads.
  const description = agent.session?.description ?? asString(calls[0]?.input["description"]);
  const name = agent.session?.agent ?? asString(calls[0]?.input["agent"]);
  const last = agent.session?.last;

  // A child with no run and no finished turn is still starting while a call
  // of the parent's for it (its start or a resume) or its job is alive, and
  // was interrupted otherwise.
  const starting = calls.some((call) => call.runtime?.pending) || Boolean(job);
  const status = agent.live ? (
    <StatusPill>
      running
      {agent.turnStartedAt && (
        <>
          {" "}
          · <Elapsed since={agent.turnStartedAt} />
        </>
      )}
    </StatusPill>
  ) : !agent.session ? null : last ? (
    <StatusPill tone={last.status === "failed" ? "error" : "muted"}>
      {formatAgentOutcome(last)}
    </StatusPill>
  ) : (
    <StatusPill>{starting ? "starting" : "interrupted"}</StatusPill>
  );

  // The transcript's cards open other children of the parent; a subagent
  // runs no background commands, so it has no logs to link.
  const links = useMemo<SessionLinks>(
    () => ({ openAgent: (id) => onOpen({ sessionId: parentId, childId: id }) }),
    [onOpen, parentId],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/30 px-4 py-2.5 max-md:pt-[calc(0.625rem+env(safe-area-inset-top))] md:px-6">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <Bot className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <h2
              className="truncate text-sm leading-5 font-semibold text-foreground"
              title={description || "Agent"}
            >
              {description || "Agent"}
            </h2>
          </div>
          <div className="mt-0.5 flex h-4.5 min-w-0 items-center gap-1.5 pl-5 text-2xs text-muted-foreground">
            {name && <span className="shrink-0 font-mono">{name}</span>}
            {status}
          </div>
        </div>
        {job && (
          <Button
            variant="ghost"
            size="xs"
            title="Stop the agent"
            onClick={() => onStopJob(job.tool_use_id)}
          >
            <Square data-icon="inline-start" className="fill-current" />
            Stop
          </Button>
        )}
        {close}
      </header>
      {agent.error ? (
        <Notice>{agent.error}</Notice>
      ) : agent.messages.length > 0 ? (
        <SessionLinksContext value={links}>
          <MessageList
            sessionId={`${parentId}/${childId}`}
            messages={agent.messages}
            loading={agent.live}
            compacting={false}
            compactError={null}
            sendError={null}
          />
        </SessionLinksContext>
      ) : (
        agent.session && starting && <Notice>Waiting for the agent to start…</Notice>
      )}
    </div>
  );
}
