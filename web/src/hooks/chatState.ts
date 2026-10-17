/**
 * The chat reducer behind useChat and the pure helpers that feed it: request
 * input blocks, pending items, draft sessions, and run event routing.
 */

import type { BackgroundJobInfo, PendingMessage, RunEvent, RunKind } from "@/api";
import type {
  AttachedFile,
  ChatMessage,
  ComposerSubmission,
  MessageMeta,
  PendingInput,
  PendingInputs,
  PermissionRequest,
  SessionSummary,
  ToolRuntime,
  UsageTotals,
  WorkspaceFileReference,
} from "@/types";
import { randomId } from "@/utils/id";
import {
  appendAssistantDelta,
  appendToolResult,
  appendToolUse,
  createAssistantMessage,
  createUserMessage,
  createUserTextMessage,
  isJobBlock,
  markTailAssistantStopped,
  readUsageTotals,
  updateLatestAssistantMeta,
  updateLatestThinkingDuration,
} from "@/utils/messages";

export interface ChatState {
  messageSessionId: string | null;
  rawMessages: ChatMessage[];
  toolRuntimeById: Record<string, ToolRuntime>;
  /** Session cumulative usage and cost; null when unknown. Set on session
   * load, updated live by SSE usage events. */
  sessionUsage: UsageTotals | null;
  /** Snapshot of rawMessages taken before the latest optimistic turn.
   * Used by 'rollback' to restore state when the request fails. */
  preTurnRawMessages: ChatMessage[] | null;
  /** Steers and queued messages handed to the running chat and not yet
   * delivered by a `user_message` event. */
  pending: PendingInputs;
  /** Background commands running in this session, from the snapshot and the job events. */
  jobs: BackgroundJobInfo[];
  /** Tool calls waiting for approval, oldest first. */
  permissions: PermissionRequest[];
}

const NO_PENDING: PendingInputs = { steers: [], queue: [] };

export const INITIAL_CHAT_STATE: ChatState = {
  messageSessionId: null,
  rawMessages: [],
  toolRuntimeById: {},
  sessionUsage: null,
  preTurnRawMessages: null,
  pending: NO_PENDING,
  jobs: [],
  permissions: [],
};

export type ChatAction =
  | {
      type: "set_messages";
      messages: ChatMessage[];
      sessionId?: string | null;
      sessionUsage?: UsageTotals | null;
      replayEvents?: RunEvent[];
      expectedSessionId?: string | null;
      pending?: PendingInputs;
      jobs?: BackgroundJobInfo[];
    }
  | {
      type: "start_turn";
      content: string;
      attachments?: AttachedFile[];
      workspaceFiles?: WorkspaceFileReference[];
    }
  | { type: "rewind_and_start_turn"; rewindTo: number; content: string }
  | { type: "accept_turn"; message: ChatMessage }
  | { type: "apply_event"; event: RunEvent }
  | { type: "rollback" }
  | { type: "job_started"; job: BackgroundJobInfo }
  | { type: "job_finished"; toolUseId: string }
  | { type: "add_pending"; list: keyof PendingInputs; item: PendingInput }
  | { type: "move_pending"; id: string; to: keyof PendingInputs }
  | { type: "remove_pending"; id: string }
  | { type: "clear_pending" }
  | { type: "clear_permissions" };

/** Runtime of a tool whose first event just arrived. */
const NEW_TOOL_RUNTIME: ToolRuntime = {
  pending: true,
  output: "",
  finalOutput: null,
  metadata: null,
  isError: false,
};

export function createDraftSession(): SessionSummary {
  return { id: randomId(), title: "New chat", isDraft: true };
}

/** The session to open in a workspace: the one last active there, else the latest. */
export function resolveInitialSessionId(
  sessions: SessionSummary[],
  preferredSessionId: string,
): string | null {
  return (sessions.find((session) => session.id === preferredSessionId) ?? sessions[0])?.id ?? null;
}

/**
 * Sort a followed run's events, live or replayed from a snapshot: which reach
 * the chat state, whether the run ended stopped or failed, and a compact
 * run's failure. A compact run surfaces only its marker; its completed stream
 * reloads persisted history and session cost.
 */
export function routeRunEvents(
  kind: RunKind,
  events: RunEvent[],
): { apply: RunEvent[]; stopped: boolean; compactError: string | null } {
  const apply: RunEvent[] = [];
  let stopped = false;
  let compactError: string | null = null;
  for (const event of events) {
    if (event.type === "cancelled" || event.type === "error") stopped = true;
    if (kind === "compact") {
      if (event.type === "compact") apply.push(event);
      else if (event.type === "error") compactError = event.message || "Compaction failed";
      continue;
    }
    apply.push(event);
  }
  return { apply, stopped, compactError };
}

/** Map a pending upload attachment to a /api/chat input block. */
function attachmentToInputBlock(attachment: AttachedFile): Record<string, unknown> {
  if (attachment.kind === "text") {
    return {
      type: "text",
      text: attachment.text,
      name: attachment.name,
      is_attachment: true,
    };
  }
  return {
    type: attachment.kind === "image" ? "image" : "document",
    data: attachment.data,
    mime_type: attachment.mime_type,
    name: attachment.name,
  };
}

/** Map an inline @ workspace reference to a /api/chat path input block. */
function workspaceRefToInputBlock(ref: WorkspaceFileReference): Record<string, unknown> {
  if (ref.kind === "text") {
    // Server reads the file into the same <file> snapshot as CLI @file;
    // the path doubles as the display name.
    return {
      type: "text",
      path: ref.path,
      name: ref.path,
      is_attachment: true,
    };
  }
  return {
    type: ref.kind,
    path: ref.path,
    name: ref.name,
    is_attachment: true,
  };
}

/** Drop duplicate @ references so a file only enters the context once. */
export function dedupeWorkspaceFiles(refs: WorkspaceFileReference[]): WorkspaceFileReference[] {
  const seen = new Set<string>();
  return refs.filter((ref) => {
    const key = `${ref.kind}:${ref.path}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Request input blocks for a submission, the shape /api/chat takes. */
export function buildInputBlocks(
  content: string,
  workspaceFiles: WorkspaceFileReference[],
  attachments: AttachedFile[],
): Record<string, unknown>[] {
  return [
    ...(content ? [{ type: "text", text: content }] : []),
    ...workspaceFiles.map(workspaceRefToInputBlock),
    ...attachments.map(attachmentToInputBlock),
  ];
}

export function createPendingInput(
  submission: ComposerSubmission,
  attachments: AttachedFile[] = [],
): PendingInput | null {
  const text = submission.text.trim();
  const workspaceFiles = submission.workspaceFiles;
  if (!text && !workspaceFiles.length && !attachments.length) return null;
  return {
    id: randomId(),
    submission: { text, workspaceFiles },
    attachments,
    input: buildInputBlocks(text, dedupeWorkspaceFiles(workspaceFiles), attachments),
  };
}

/** A pending message from a session snapshot. Only its typed text survives:
 * skills expand again on resend, but the attachments the server built are
 * not the composer's uploads and references, so the item is partial. */
export function pendingFromMessage(message: PendingMessage): PendingInput {
  const texts: string[] = [];
  let partial = false;
  for (const block of message.content) {
    const meta = block.meta ?? {};
    if (meta["skill_snapshot"]) continue;
    if (block.type !== "text" || meta["attachment"]) partial = true;
    else if (block.text) texts.push(block.text);
  }
  const text = texts.join("\n\n");
  return {
    id: message.meta.input_id,
    submission: { text, workspaceFiles: [] },
    attachments: [],
    input: text ? [{ type: "text", text }] : [],
    ...(partial ? { partial } : {}),
  };
}

function withoutPending(pending: PendingInputs, ids: ReadonlySet<string>): PendingInputs {
  const keep = (item: PendingInput) => !ids.has(item.id);
  return {
    steers: pending.steers.filter(keep),
    queue: pending.queue.filter(keep),
  };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "set_messages": {
      if (action.expectedSessionId != null && state.messageSessionId !== action.expectedSessionId) {
        return state;
      }

      let nextState: ChatState = {
        messageSessionId: action.sessionId ?? state.messageSessionId,
        rawMessages: action.messages,
        toolRuntimeById: {},
        sessionUsage: action.sessionUsage ?? null,
        preTurnRawMessages: null,
        pending: action.pending ?? NO_PENDING,
        jobs: action.jobs ?? [],
        permissions: [],
      };

      for (const event of action.replayEvents ?? []) {
        nextState = chatReducer(nextState, { type: "apply_event", event });
      }

      return nextState;
    }

    case "start_turn": {
      const { content, attachments, workspaceFiles } = action;
      // PDFs can be large enough to freeze history rendering after a failed send.
      const uiAttachments = attachments?.map((attachment) =>
        attachment.kind === "document" ? { ...attachment, data: "" } : attachment,
      );
      const hasBlocks = Boolean(uiAttachments?.length || workspaceFiles?.length);
      return {
        ...state,
        rawMessages: [
          ...state.rawMessages,
          hasBlocks
            ? createUserMessage(content, uiAttachments ?? [], workspaceFiles)
            : createUserTextMessage(content),
          createAssistantMessage([]),
        ],
        preTurnRawMessages: state.rawMessages,
      };
    }

    case "rewind_and_start_turn": {
      return {
        ...state,
        rawMessages: [
          ...state.rawMessages.slice(0, action.rewindTo),
          createUserTextMessage(action.content),
          createAssistantMessage([]),
        ],
        toolRuntimeById: {},
        preTurnRawMessages: state.rawMessages,
      };
    }

    case "accept_turn": {
      // The server's copy of the sent message carries the background results
      // that waited for it; the client's own rendering of the input stays.
      const results = action.message.content.filter(isJobBlock);
      const index = state.rawMessages.findLastIndex((m) => m.role === "user");
      const message = state.rawMessages[index];
      if (!results.length || !message) return state;
      return {
        ...state,
        rawMessages: state.rawMessages.with(index, {
          ...message,
          content: [...results, ...message.content],
        }),
      };
    }

    case "job_started": {
      const { job } = action;
      if (state.jobs.some((item) => item.tool_use_id === job.tool_use_id)) {
        return state;
      }
      return { ...state, jobs: [...state.jobs, job] };
    }

    case "job_finished":
      return {
        ...state,
        jobs: state.jobs.filter((job) => job.tool_use_id !== action.toolUseId),
      };

    case "rollback": {
      const snapshot = state.preTurnRawMessages;
      if (!snapshot) return state;
      return {
        ...state,
        rawMessages: snapshot,
        toolRuntimeById: {},
        preTurnRawMessages: null,
      };
    }

    case "add_pending": {
      const { list, item } = action;
      return {
        ...state,
        pending: { ...state.pending, [list]: [...state.pending[list], item] },
      };
    }

    case "move_pending": {
      const { id, to } = action;
      const item = [...state.pending.steers, ...state.pending.queue].find(
        (pending) => pending.id === id,
      );
      if (!item) return state;
      const pending = withoutPending(state.pending, new Set([id]));
      return {
        ...state,
        pending: { ...pending, [to]: [...pending[to], item] },
      };
    }

    case "remove_pending":
      return {
        ...state,
        pending: withoutPending(state.pending, new Set([action.id])),
      };

    case "clear_pending":
      return { ...state, pending: NO_PENDING };

    case "clear_permissions":
      return state.permissions.length ? { ...state, permissions: [] } : state;

    case "apply_event": {
      const { event } = action;
      let rawMessages = state.rawMessages;
      const toolRuntimeById = { ...state.toolRuntimeById };

      if (event.type === "reasoning") {
        rawMessages = appendAssistantDelta(rawMessages, "thinking", event.delta);
      } else if (event.type === "reasoning_done") {
        rawMessages = updateLatestThinkingDuration(rawMessages, event.duration_ms);
      } else if (event.type === "text") {
        rawMessages = appendAssistantDelta(rawMessages, "text", event.delta);
      } else if (event.type === "tool_start") {
        rawMessages = appendToolUse(rawMessages, event.tool_call);
        toolRuntimeById[event.tool_call.id] = NEW_TOOL_RUNTIME;
      } else if (event.type === "tool_output") {
        const current = toolRuntimeById[event.tool_use_id] ?? NEW_TOOL_RUNTIME;
        toolRuntimeById[event.tool_use_id] = {
          ...current,
          pending: true,
          output: `${current.output}${event.output}`,
        };
      } else if (event.type === "tool_done") {
        const { tool_use_id: toolUseId, output: finalOutput } = event;
        const metadata = event.metadata ?? null;
        const isError = event.is_error || finalOutput.startsWith("error:");
        toolRuntimeById[toolUseId] = {
          ...(toolRuntimeById[toolUseId] ?? NEW_TOOL_RUNTIME),
          pending: false,
          finalOutput,
          metadata,
          isError,
        };
        rawMessages = appendToolResult(rawMessages, toolUseId, finalOutput, metadata, isError);
      } else if (event.type === "error") {
        rawMessages = markTailAssistantStopped(
          rawMessages,
          "error",
          event.message || "Unknown error",
        );
      } else if (event.type === "cancelled") {
        for (const [id, runtime] of Object.entries(toolRuntimeById)) {
          if (runtime.pending) {
            toolRuntimeById[id] = { ...runtime, pending: false, isError: true };
          }
        }
        rawMessages = markTailAssistantStopped(rawMessages, "cancelled");
        return { ...state, rawMessages, toolRuntimeById, permissions: [] };
      } else if (event.type === "usage") {
        // The event carries the turn's cumulative values, so replace the
        // previous snapshot instead of summing it again.
        const patch: Partial<MessageMeta> = {
          context_tokens: event.context_tokens ?? null,
          turn_usage: event.turn_usage ?? {},
          turn_cost: event.turn_cost ?? null,
          turn_duration_ms: event.turn_duration_ms ?? null,
        };
        if (event.context_window !== undefined) patch.context_window = event.context_window;
        if (event.model) patch.model = event.model;
        rawMessages = updateLatestAssistantMeta(rawMessages, patch);
        return {
          ...state,
          rawMessages,
          toolRuntimeById,
          sessionUsage: readUsageTotals(event.session_usage, event.session_cost),
        };
      } else if (event.type === "compact") {
        rawMessages = [
          ...rawMessages,
          { role: "compact", content: [], meta: { trigger: event.trigger } },
        ];
      } else if (event.type === "user_message") {
        // A delivered steer or queued turn opens a new segment, the same
        // shape start_turn creates; later usage events patch its assistant.
        return {
          ...state,
          rawMessages: [...rawMessages, event.message, createAssistantMessage([])],
          toolRuntimeById,
          pending: withoutPending(state.pending, new Set(event.message.meta?.input_ids ?? [])),
        };
      } else if (event.type === "permission_request") {
        const { request_id, tool_use_id, tool_name, preview } = event;
        if (state.permissions.some((request) => request.request_id === request_id)) return state;
        return {
          ...state,
          permissions: [...state.permissions, { request_id, tool_use_id, tool_name, preview }],
        };
      } else if (event.type === "permission_resolved") {
        return {
          ...state,
          permissions: state.permissions.filter(
            (request) => request.request_id !== event.request_id,
          ),
        };
      }

      return { ...state, rawMessages, toolRuntimeById };
    }
    default:
      return state;
  }
}
