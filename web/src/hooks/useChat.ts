/**
 * Chat state management hook.
 * Keeps large document payloads out of React state; the request body still sends them.
 */

import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  AttachedFile,
  ChatErrorResponse,
  ChatMessage,
  ChatResponse,
  CompactResponse,
  ComposerSubmission,
  LocalConfig,
  MessageMeta,
  PendingInput,
  PendingInputs,
  PendingMessage,
  PermissionRequest,
  RemoteConfig,
  RunInfo,
  RunKind,
  SessionResponse,
  SessionSummary,
  SessionsResponse,
  StreamEvent,
  ToolRuntime,
  UsageTotals,
  WorkspaceFileReference,
} from "../types";
import { isCompactMarker } from "../types";
import { getReasoningEffortOverride } from "../utils/config";
import { randomId } from "../utils/id";
import {
  appendAssistantDelta,
  appendToolResult,
  appendToolUse,
  buildRenderMessages,
  createAssistantMessage,
  createUserMessage,
  createUserTextMessage,
  markTailAssistantStopped,
  readUsageTotals,
  updateLatestAssistantMeta,
  updateLatestThinkingDuration,
} from "../utils/messages";
import {
  loadActiveSession,
  removeActiveSession,
  saveActiveSession,
} from "../utils/storage";
import {
  isCurrentSendRequest,
  isCurrentWorkspaceRequest,
  resolveInitialSessionId,
} from "./sessionSelection";

const DEFAULT_SESSION_TITLE = "New chat";

interface ChatState {
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
}

const NO_PENDING: PendingInputs = { steers: [], queue: [] };

const INITIAL_CHAT_STATE: ChatState = {
  messageSessionId: null,
  rawMessages: [],
  toolRuntimeById: {},
  sessionUsage: null,
  preTurnRawMessages: null,
  pending: NO_PENDING,
};

type ChatAction =
  | {
      type: "set_messages";
      messages: ChatMessage[];
      sessionId?: string | null;
      sessionUsage?: UsageTotals | null;
      replayEvents?: StreamEvent[];
      expectedSessionId?: string | null;
      pending?: PendingInputs;
    }
  | {
      type: "start_turn";
      content: string;
      attachments?: AttachedFile[];
      workspaceFiles?: WorkspaceFileReference[];
    }
  | { type: "rewind_and_start_turn"; rewindTo: number; content: string }
  | { type: "apply_event"; event: StreamEvent }
  | { type: "rollback" }
  | { type: "add_pending"; list: keyof PendingInputs; item: PendingInput }
  | { type: "move_pending"; id: string; to: keyof PendingInputs }
  | { type: "remove_pending"; id: string }
  | { type: "clear_pending" };

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

function getErrorDetail(
  data: ChatResponse | CompactResponse | ChatErrorResponse,
): ChatErrorResponse["detail"] {
  return "detail" in data ? data.detail : undefined;
}

function getRunFromDetail(detail: ChatErrorResponse["detail"]): RunInfo | null {
  if (Array.isArray(detail)) return null;
  return typeof detail === "object" && detail?.run ? detail.run : null;
}

function getMessageFromDetail(
  detail: ChatErrorResponse["detail"],
  fallback: string,
): string {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const firstMessage = detail.find((item) => item.msg)?.msg;
    return firstMessage || fallback;
  }
  if (detail && typeof detail === "object" && detail.message) {
    return detail.message;
  }
  return fallback;
}

function createDraftSession(): SessionSummary {
  return { id: randomId(), title: DEFAULT_SESSION_TITLE, isDraft: true };
}

/** Map a pending upload attachment to a /api/chat input block. */
function attachmentToInputBlock(
  attachment: AttachedFile,
): Record<string, unknown> {
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
function workspaceRefToInputBlock(
  ref: WorkspaceFileReference,
): Record<string, unknown> {
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
function dedupeWorkspaceFiles(
  refs: WorkspaceFileReference[],
): WorkspaceFileReference[] {
  const seen = new Set<string>();
  return refs.filter((ref) => {
    const key = `${ref.kind}:${ref.path}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Request input blocks for a submission, the shape /api/chat takes. */
function buildInputBlocks(
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

function createPendingInput(
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
    input: buildInputBlocks(
      text,
      dedupeWorkspaceFiles(workspaceFiles),
      attachments,
    ),
  };
}

/** A pending message from a session snapshot. Only its typed text survives:
 * skills expand again on resend, but the attachments the server built are
 * not the composer's uploads and references, so the item is partial. */
function pendingFromMessage(message: PendingMessage): PendingInput {
  const texts: string[] = [];
  let partial = false;
  for (const block of message.content) {
    const meta = block.meta ?? {};
    // biome-ignore lint/complexity/useLiteralKeys: index signature requires bracket access
    if (meta["skill_snapshot"]) continue;
    // biome-ignore lint/complexity/useLiteralKeys: index signature requires bracket access
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

function withoutPending(
  pending: PendingInputs,
  ids: ReadonlySet<string>,
): PendingInputs {
  const keep = (item: PendingInput) => !ids.has(item.id);
  return {
    steers: pending.steers.filter(keep),
    queue: pending.queue.filter(keep),
  };
}

function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "set_messages": {
      if (
        action.expectedSessionId != null &&
        state.messageSessionId !== action.expectedSessionId
      ) {
        return state;
      }

      let nextState: ChatState = {
        messageSessionId: action.sessionId ?? state.messageSessionId,
        rawMessages: action.messages,
        toolRuntimeById: {},
        sessionUsage: action.sessionUsage ?? null,
        preTurnRawMessages: null,
        pending: action.pending ?? NO_PENDING,
      };

      for (const event of action.replayEvents || []) {
        nextState = chatReducer(nextState, { type: "apply_event", event });
      }

      return nextState;
    }

    case "start_turn": {
      const { content, attachments, workspaceFiles } = action;
      // PDFs can be large enough to freeze history rendering after a failed send.
      const uiAttachments = attachments?.map((attachment) =>
        attachment.kind === "document"
          ? { ...attachment, data: "" }
          : attachment,
      );
      const hasBlocks = Boolean(
        uiAttachments?.length || workspaceFiles?.length,
      );
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

    case "apply_event": {
      const { event } = action;
      let rawMessages = state.rawMessages;
      const toolRuntimeById = { ...state.toolRuntimeById };

      if (event.type === "reasoning") {
        rawMessages = appendAssistantDelta(
          rawMessages,
          "thinking",
          event.delta || "",
        );
      } else if (event.type === "reasoning_done") {
        const durationMs = event.duration_ms;
        if (typeof durationMs === "number") {
          rawMessages = updateLatestThinkingDuration(rawMessages, durationMs);
        }
      } else if (event.type === "text") {
        rawMessages = appendAssistantDelta(
          rawMessages,
          "text",
          event.delta || "",
        );
      } else if (event.type === "tool_start") {
        const toolCall = event.tool_call || {};
        rawMessages = appendToolUse(rawMessages, toolCall);
        if (toolCall.id) {
          toolRuntimeById[toolCall.id] = {
            pending: true,
            output: "",
            finalOutput: null,
            metadata: null,
            isError: false,
          };
        }
      } else if (event.type === "tool_output") {
        const toolUseId = event.tool_use_id || "";
        if (toolUseId) {
          const current = toolRuntimeById[toolUseId] || {
            pending: true,
            output: "",
            finalOutput: null,
            metadata: null,
            isError: false,
          };
          const nextOutput = event.output || "";
          toolRuntimeById[toolUseId] = {
            ...current,
            pending: true,
            output: `${current.output}${nextOutput}`,
          };
        }
      } else if (event.type === "tool_done") {
        const toolUseId = event.tool_use_id || "";
        const finalOutput = event.output || "";
        const metadata = event.metadata ?? null;
        const isError = Boolean(
          event.is_error ||
            (typeof finalOutput === "string" &&
              finalOutput.startsWith("error:")),
        );

        if (toolUseId) {
          const current = toolRuntimeById[toolUseId] || {
            pending: false,
            output: "",
            finalOutput: null,
            metadata: null,
            isError: false,
          };
          toolRuntimeById[toolUseId] = {
            ...current,
            pending: false,
            finalOutput,
            metadata,
            isError,
          };
          rawMessages = appendToolResult(
            rawMessages,
            toolUseId,
            finalOutput,
            metadata,
            isError,
          );
        }
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
      } else if (event.type === "usage") {
        // The event carries the turn's cumulative values, so replace the
        // previous snapshot instead of summing it again.
        const patch: Partial<MessageMeta> = {
          context_tokens: event.context_tokens ?? null,
          turn_usage: event.turn_usage ?? {},
          turn_cost: event.turn_cost ?? null,
          turn_duration_ms: event.turn_duration_ms ?? null,
        };
        if (typeof event.context_window === "number") {
          patch.context_window = event.context_window;
        }
        if (event.model) patch.model = event.model;
        rawMessages = updateLatestAssistantMeta(rawMessages, patch);
        return {
          ...state,
          rawMessages,
          toolRuntimeById,
          sessionUsage: readUsageTotals(
            event.session_usage,
            event.session_cost,
          ),
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
          rawMessages: [
            ...rawMessages,
            event.message,
            createAssistantMessage([]),
          ],
          toolRuntimeById,
          pending: withoutPending(
            state.pending,
            new Set(event.message.meta?.input_ids ?? []),
          ),
        };
      }

      return { ...state, rawMessages, toolRuntimeById };
    }
    default:
      return state;
  }
}

/**
 * @param onRestore Receives pending items that go back into the composer:
 *   after a stop or failure, or when their fallback send is rejected.
 */
export function useChat(
  config: LocalConfig,
  remoteConfig?: RemoteConfig | null,
  onRestore?: (items: PendingInput[]) => void,
) {
  const [chatState, setChatState] = useState(INITIAL_CHAT_STATE);
  // The reducer runs eagerly into this ref so async request and stream code
  // reads the current pending list, not the one from the last render.
  const chatStateRef = useRef(chatState);
  const dispatch = useCallback((action: ChatAction) => {
    chatStateRef.current = chatReducer(chatStateRef.current, action);
    setChatState(chatStateRef.current);
  }, []);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSession, setActiveSession] = useState(createDraftSession);
  // Kind of the run this client is following; null when idle. `loading` is
  // derived so chat and compact runs share the busy/cancel plumbing.
  const [runKind, setRunKind] = useState<RunKind | null>(null);
  const [compactError, setCompactError] = useState<string | null>(null);
  // A rejected send is a request failure, not the outcome of any turn.
  const [sendError, setSendError] = useState<string | null>(null);
  const loading = runKind !== null;
  const [sessionLoading, setSessionLoading] = useState(false);
  const [pendingPermissions, setPendingPermissions] = useState<
    PermissionRequest[]
  >([]);
  const initRef = useRef(false);
  const cwdRef = useRef(config.cwd);
  const activeSessionRef = useRef(activeSession);
  const requestTokenRef = useRef(0);
  const pendingRequestTokenRef = useRef(0);
  /** Token of a turn stopped while its `POST /api/chat` was in flight; the
   * run it names is stopped as soon as the response arrives. */
  const stoppedRequestTokenRef = useRef(0);
  const sessionRequestTokenRef = useRef(0);
  const streamAbortRef = useRef<AbortController | null>(null);
  const streamTokenRef = useRef(0);
  const activeRunRef = useRef<RunInfo | null>(null);
  const onRestoreRef = useRef(onRestore);
  const sendPendingRef = useRef<((items: PendingInput[]) => void) | null>(null);
  /** Steer and queue requests in flight. A stream end waits for them before
   * deciding what is still pending. */
  const inflightRef = useRef(new Set<Promise<void>>());
  const loadSessionRef = useRef<
    | ((
        sessionId: string,
        options?: { requestCwd?: string; requestToken?: number },
      ) => Promise<SessionResponse | null>)
    | null
  >(null);

  useEffect(() => {
    onRestoreRef.current = onRestore;
  }, [onRestore]);

  /** Remove and return every pending item, steers first. */
  const takePending = useCallback((): PendingInput[] => {
    const { steers, queue } = chatStateRef.current.pending;
    if (steers.length || queue.length) dispatch({ type: "clear_pending" });
    return [...steers, ...queue];
  }, [dispatch]);

  const isPending = useCallback((id: string) => {
    const { steers, queue } = chatStateRef.current.pending;
    return [...steers, ...queue].some((item) => item.id === id);
  }, []);

  const restorePending = useCallback((items: PendingInput[]) => {
    if (items.length) onRestoreRef.current?.(items);
  }, []);

  /** A steer or queue request failed: the item goes back to the composer. */
  const failPending = useCallback(
    (item: PendingInput, message: string) => {
      if (!isPending(item.id)) return;
      dispatch({ type: "remove_pending", id: item.id });
      restorePending([item]);
      setSendError(message);
    },
    [dispatch, isPending, restorePending],
  );

  const setActiveSessionSnapshot = useCallback((session: SessionSummary) => {
    activeSessionRef.current = session;
    setActiveSession(session);
  }, []);

  const cancelRun = useCallback(async (runId: string) => {
    if (!runId) return;

    try {
      await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
      });
    } catch (e) {
      console.error("Failed to cancel:", e);
    }
  }, []);

  const fetchSessions = useCallback(async (): Promise<SessionSummary[]> => {
    const requestCwd = config.cwd;
    if (requestCwd !== cwdRef.current) return [];

    try {
      const res = await fetch(
        `/api/sessions?cwd=${encodeURIComponent(requestCwd)}`,
      );
      if (!res.ok) throw new Error("Failed to load sessions");
      const data = (await res.json()) as SessionsResponse;
      if (requestCwd !== cwdRef.current) {
        return [];
      }
      const savedSessions = data.sessions || [];
      const active = activeSessionRef.current;
      const sessionsWithDraft =
        active.isDraft &&
        !savedSessions.some((session) => session.id === active.id)
          ? [active, ...savedSessions]
          : savedSessions;

      setSessions(sessionsWithDraft);

      const syncedActive = sessionsWithDraft.find(
        (session) => session.id === active.id,
      );
      if (syncedActive && syncedActive !== active) {
        setActiveSessionSnapshot(syncedActive);
      }

      return sessionsWithDraft;
    } catch (e) {
      console.error("Failed to load sessions:", e);
      return [];
    }
  }, [config.cwd, setActiveSessionSnapshot]);

  const stopStreaming = useCallback(() => {
    streamTokenRef.current += 1;
    pendingRequestTokenRef.current = 0;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeRunRef.current = null;
    setRunKind(null);
    setCompactError(null);
    setSendError(null);
    setPendingPermissions([]);
  }, []);

  const streamRun = useCallback(
    async (
      run: RunInfo,
      sessionId: string,
      after = 0,
      interrupted = false,
    ): Promise<void> => {
      const runId = run?.id;
      if (!runId) return;
      // Stopped or failed runs return pending input to the composer; a run
      // that ended normally sends it as the next turn.
      let stopped = interrupted;
      let sawDone = false;

      streamTokenRef.current += 1;
      const token = streamTokenRef.current;
      streamAbortRef.current?.abort();

      const controller = new AbortController();
      streamAbortRef.current = controller;
      activeRunRef.current = run;
      const kind = run.kind;
      setRunKind(kind);
      // Whether this stream still drives the UI: no newer stream, same session.
      const isCurrent = () =>
        streamTokenRef.current === token &&
        activeSessionRef.current.id === sessionId;

      const recoverSession = async () => {
        if (!isCurrent()) return true;

        const reload = loadSessionRef.current;
        if (!reload) {
          return false;
        }

        try {
          await reload(sessionId);
          return true;
        } catch (error) {
          console.error("Failed to recover disconnected stream:", error);
          return false;
        }
      };

      try {
        const res = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`,
          { signal: controller.signal },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        if (!res.body) throw new Error("Response body is empty");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const data = line.slice(6);
            if (data === "[DONE]") {
              sawDone = true;
              continue;
            }

            try {
              const event = JSON.parse(data) as StreamEvent;
              if (!isCurrent()) continue;
              if (event.type === "permission_request") {
                const next: PermissionRequest = {
                  request_id: event.request_id,
                  tool_use_id: event.tool_use_id,
                  tool_name: event.tool_name,
                  preview: event.preview,
                };
                setPendingPermissions((prev) =>
                  prev.some((p) => p.request_id === next.request_id)
                    ? prev
                    : [...prev, next],
                );
                continue;
              }
              if (event.type === "permission_resolved") {
                const requestId = event.request_id;
                setPendingPermissions((prev) =>
                  prev.filter((p) => p.request_id !== requestId),
                );
                continue;
              }
              if (event.type === "cancelled") {
                setPendingPermissions([]);
              }
              if (event.type === "cancelled" || event.type === "error") {
                stopped = true;
              }
              if (kind === "compact") {
                // A compact run only surfaces its marker or failure here. The
                // completed stream reloads persisted history and session cost.
                if (event.type === "compact") {
                  dispatch({ type: "apply_event", event });
                } else if (event.type === "error") {
                  console.error("Compaction failed:", event.message);
                  setCompactError(event.message || "Compaction failed");
                }
                continue;
              }
              dispatch({ type: "apply_event", event });
            } catch (e) {
              console.error("Parse error:", e);
            }
          }
        }

        if (kind === "compact" && sawDone) {
          await recoverSession();
        }

        if (!sawDone) {
          const recovered = await recoverSession();
          if (recovered) {
            return;
          }
        }
      } catch (e) {
        if (!(e instanceof Error) || e.name !== "AbortError") {
          const recovered = await recoverSession();
          if (!recovered && isCurrent()) {
            const message =
              "Stream disconnected. Reload the session to resume.";
            stopped = true;
            if (kind === "compact") {
              setCompactError(message);
            } else {
              dispatch({
                type: "apply_event",
                event: { type: "error", message },
              });
            }
          }
        }
      } finally {
        if (streamTokenRef.current === token) {
          streamAbortRef.current = null;
          activeRunRef.current = null;

          if (activeSessionRef.current.id === sessionId) {
            if (kind === "chat" && (sawDone || stopped)) {
              // The run stays busy until its pending input is settled, so a
              // send cannot race the leftovers' own request.
              await Promise.all(inflightRef.current);
              if (isCurrent()) {
                setRunKind(null);
                const items = takePending();
                if (stopped) restorePending(items);
                else if (items.length) sendPendingRef.current?.(items);
              }
            } else {
              setRunKind(null);
            }
          }

          fetchSessions();
        }
      }
    },
    [fetchSessions, restorePending, takePending, dispatch],
  );

  const loadSession = useCallback(
    async (
      sessionId: string,
      options?: { requestCwd?: string; requestToken?: number },
    ): Promise<SessionResponse | null> => {
      const requestCwd = options?.requestCwd ?? config.cwd;
      const requestToken =
        options?.requestToken ?? sessionRequestTokenRef.current;
      const isStillCurrent = () =>
        isCurrentWorkspaceRequest({
          pendingRequestToken: sessionRequestTokenRef.current,
          requestToken,
          activeCwd: cwdRef.current,
          requestCwd,
        });

      if (!isStillCurrent()) return null;

      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
      if (!res.ok) throw new Error("Failed to load session");

      const data = (await res.json()) as SessionResponse;
      if (!isStillCurrent()) return null;
      if (!data.session) return null;

      setActiveSessionSnapshot(data.session);
      saveActiveSession(requestCwd, data.session.id);
      setPendingPermissions([]);
      const run = data.active_run || null;

      const pendingEvents = Array.isArray(data.pending_events)
        ? data.pending_events
        : [];
      const isCompactRun = run?.kind === "compact";
      let replayStopped = false;
      const replayedPermissions = new Map<string, PermissionRequest>();
      const replayEvents: StreamEvent[] = [];
      for (const event of pendingEvents) {
        if (event?.type === "permission_request") {
          replayedPermissions.set(event.request_id, {
            request_id: event.request_id,
            tool_use_id: event.tool_use_id,
            tool_name: event.tool_name,
            preview: event.preview,
          });
          continue;
        }
        if (event?.type === "permission_resolved") {
          replayedPermissions.delete(event.request_id);
          continue;
        }
        if (event?.type === "cancelled") {
          replayedPermissions.clear();
        }
        if (event?.type === "cancelled" || event?.type === "error") {
          replayStopped = true;
        }
        if (isCompactRun) {
          // Same routing as the live stream: only the marker reaches history.
          if (event?.type === "compact") replayEvents.push(event);
          else if (event?.type === "error") {
            setCompactError(event.message || "Compaction failed");
          }
          continue;
        }
        replayEvents.push(event);
      }
      startTransition(() => {
        dispatch({
          type: "set_messages",
          messages: data.messages || [],
          sessionId: data.session?.id ?? sessionId,
          sessionUsage: readUsageTotals(data.session_usage, data.session_cost),
          replayEvents,
          expectedSessionId: data.session?.id ?? sessionId,
          pending: {
            steers: (data.pending?.steers ?? []).map(pendingFromMessage),
            queue: (data.pending?.queue ?? []).map(pendingFromMessage),
          },
        });
      });
      if (replayedPermissions.size) {
        setPendingPermissions(Array.from(replayedPermissions.values()));
      }

      activeRunRef.current = run;

      if (run?.id) {
        const lastSeq = pendingEvents.at(-1)?.seq ?? 0;
        streamRun(run, data.session.id, lastSeq, replayStopped);
      } else {
        setRunKind(null);
      }

      return data;
    },
    [config.cwd, dispatch, setActiveSessionSnapshot, streamRun],
  );

  useEffect(() => {
    loadSessionRef.current = loadSession;
  }, [loadSession]);

  const cancel = useCallback(() => {
    const runId = activeRunRef.current?.id;
    if (!runId && pendingRequestTokenRef.current) {
      // The run is unknown until POST /api/chat returns; it is stopped then.
      stoppedRequestTokenRef.current = pendingRequestTokenRef.current;
      return;
    }
    const sessionId = activeSessionRef.current.id;

    streamTokenRef.current += 1;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeRunRef.current = null;
    setPendingPermissions([]);
    // The aborted stream never sees its `cancelled`, so undelivered input
    // goes back to the composer here. A steer committed just before the stop
    // may not have reached this client yet: the reloaded history decides.
    const items = takePending();

    if (!runId) {
      setRunKind(null);
      restorePending(items);
      return;
    }

    void (async () => {
      await cancelRun(runId);
      if (activeSessionRef.current.id !== sessionId) return;

      const delivered = new Set<string>();
      try {
        const data = await loadSessionRef.current?.(sessionId);
        for (const message of data?.messages ?? []) {
          for (const id of message.meta?.input_ids ?? []) delivered.add(id);
        }
        fetchSessions();
      } catch (error) {
        console.error("Failed to reload session after cancel:", error);
        if (activeSessionRef.current.id === sessionId) {
          setRunKind(null);
        }
      }
      // A session switched to during the reload keeps its own composer.
      if (activeSessionRef.current.id !== sessionId) return;
      restorePending(items.filter((item) => !delivered.has(item.id)));
    })();
  }, [cancelRun, fetchSessions, restorePending, takePending]);

  const postChat = useCallback(
    async (
      body: Record<string, unknown>,
      sessionId: string,
      requestCwd: string,
      requestToken: number,
      // Pending items this request sends; a rejection returns them to the
      // composer instead of attaching to another run.
      pendingItems: PendingInput[] = [],
    ): Promise<boolean> => {
      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });

        const data = (await res.json()) as ChatResponse | ChatErrorResponse;
        const isCurrentRequest = isCurrentSendRequest({
          pendingRequestToken: pendingRequestTokenRef.current,
          requestToken,
          activeSessionId: activeSessionRef.current.id,
          sessionId,
          activeCwd: cwdRef.current,
          requestCwd,
        });

        if (!res.ok) {
          // Restore original messages on failure (the turn was optimistic).
          if (isCurrentRequest) {
            pendingRequestTokenRef.current = 0;
            dispatch({ type: "rollback" });

            const detail = getErrorDetail(data);
            const existingRun = getRunFromDetail(detail);
            if (
              res.status === 409 &&
              existingRun?.id &&
              pendingItems.length === 0
            ) {
              streamRun(existingRun, sessionId, existingRun.last_seq || 0);
              return false;
            }

            restorePending(pendingItems);
            setRunKind(null);
            setSendError(getMessageFromDetail(detail, "Failed to start task"));
          }
          return false;
        }

        pendingRequestTokenRef.current = 0;
        if (!isCurrentRequest) return false;

        const chatData = data as ChatResponse;

        // Update active session from backend response (has real title, id, etc.)
        if (chatData.session) {
          setActiveSessionSnapshot(chatData.session);
          saveActiveSession(requestCwd, chatData.session.id);
        }

        // Refresh sidebar immediately so title + is_running are visible
        fetchSessions();
        streamRun(chatData.run, sessionId, 0);
        if (stoppedRequestTokenRef.current === requestToken) cancel();
        return true;
      } catch (e) {
        if (
          pendingRequestTokenRef.current === requestToken &&
          activeSessionRef.current.id === sessionId
        ) {
          pendingRequestTokenRef.current = 0;
          setRunKind(null);
          dispatch({ type: "rollback" });
          restorePending(pendingItems);
          setSendError(getErrorMessage(e));
        }
        return false;
      }
    },
    [
      cancel,
      dispatch,
      fetchSessions,
      restorePending,
      setActiveSessionSnapshot,
      streamRun,
    ],
  );

  /** Optimistic turn plus `POST /api/chat` with the current model settings. */
  const startTurn = useCallback(
    (
      turn: Extract<
        ChatAction,
        { type: "start_turn" | "rewind_and_start_turn" }
      >,
      request:
        | { message: string; rewind_to?: number }
        | { input: Record<string, unknown>[] },
      pendingItems?: PendingInput[],
    ) => {
      const sessionId = activeSessionRef.current.id;
      const requestCwd = config.cwd;
      const requestToken = requestTokenRef.current + 1;

      requestTokenRef.current = requestToken;
      pendingRequestTokenRef.current = requestToken;

      dispatch(turn);
      setRunKind("chat");
      setCompactError(null);
      setSendError(null);

      return postChat(
        {
          session_id: sessionId,
          provider: config.provider || undefined,
          model: config.model || undefined,
          cwd: config.cwd,
          reasoning_effort: getReasoningEffortOverride(config, remoteConfig),
          ...request,
        },
        sessionId,
        requestCwd,
        requestToken,
        pendingItems,
      );
    },
    [config, dispatch, postChat, remoteConfig],
  );

  const send = useCallback(
    async (
      submission: ComposerSubmission,
      attachments: AttachedFile[] = [],
    ) => {
      const content = submission.text.trim();
      const workspaceFiles = dedupeWorkspaceFiles(submission.workspaceFiles);
      if (
        (!content && !attachments.length && !workspaceFiles.length) ||
        loading
      )
        return false;

      // Use structured `input` blocks when any attachment is present.
      const request =
        attachments.length || workspaceFiles.length
          ? { input: buildInputBlocks(content, workspaceFiles, attachments) }
          : { message: content };
      return startTurn(
        {
          type: "start_turn",
          content,
          ...(attachments.length ? { attachments } : {}),
          ...(workspaceFiles.length ? { workspaceFiles } : {}),
        },
        request,
      );
    },
    [loading, startTurn],
  );

  /** Send pending items left when a run ended normally as one new turn. */
  const sendPending = useCallback(
    (items: PendingInput[]) => {
      const attachments = items.flatMap((item) => item.attachments);
      const workspaceFiles = dedupeWorkspaceFiles(
        items.flatMap((item) => item.submission.workspaceFiles),
      );
      void startTurn(
        {
          type: "start_turn",
          content: items
            .map((item) => item.submission.text)
            .filter(Boolean)
            .join("\n\n"),
          ...(attachments.length ? { attachments } : {}),
          ...(workspaceFiles.length ? { workspaceFiles } : {}),
        },
        { input: items.flatMap((item) => item.input) },
        items,
      );
    },
    [startTurn],
  );

  useEffect(() => {
    sendPendingRef.current = sendPending;
  }, [sendPending]);

  const postPendingInput = useCallback(
    (url: string, item: PendingInput) =>
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: item.input, input_id: item.id }),
      }),
    [],
  );

  const deleteQueued = useCallback(
    (sessionId: string, id: string) =>
      fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(id)}`,
        { method: "DELETE" },
      ),
    [],
  );

  const track = useCallback((request: Promise<void>) => {
    inflightRef.current.add(request);
    void request.finally(() => inflightRef.current.delete(request));
  }, []);

  // Each await below re-checks that the item is still pending: a stop, a
  // stream end, a delivery, or a session switch takes it out, and whoever
  // took it owns it from then on.
  const deliverQueued = useCallback(
    async (item: PendingInput) => {
      const sessionId = activeSessionRef.current.id;
      try {
        const res = await postPendingInput(
          `/api/sessions/${encodeURIComponent(sessionId)}/queue`,
          item,
        );
        if (res.ok) {
          // Removed while the request was in flight: take it off the server too.
          if (!isPending(item.id) && activeSessionRef.current.id === sessionId)
            void deleteQueued(sessionId, item.id);
          return;
        }
        if (!isPending(item.id)) return;
        // 409: no chat to queue on. The item stays local and goes out when
        // the stream ends.
        if (res.status === 409) return;
        const data = (await res.json()) as ChatErrorResponse;
        failPending(
          item,
          getMessageFromDetail(getErrorDetail(data), "Failed to queue"),
        );
      } catch (e) {
        failPending(item, getErrorMessage(e));
      }
    },
    [deleteQueued, failPending, isPending, postPendingInput],
  );

  const deliverSteer = useCallback(
    async (runId: string, item: PendingInput) => {
      try {
        const res = await postPendingInput(
          `/api/runs/${encodeURIComponent(runId)}/steer`,
          item,
        );
        if (!isPending(item.id) || res.ok) return;
        if (res.status === 409) {
          // The turn is ending: the item becomes the next turn instead.
          dispatch({ type: "move_pending", id: item.id, to: "queue" });
          await deliverQueued(item);
          return;
        }
        const data = (await res.json()) as ChatErrorResponse;
        failPending(
          item,
          getMessageFromDetail(getErrorDetail(data), "Failed to steer"),
        );
      } catch (e) {
        failPending(item, getErrorMessage(e));
      }
    },
    [deliverQueued, dispatch, failPending, isPending, postPendingInput],
  );

  /** Hand a message to the running chat: as a steer for its next step
   * boundary, or queued as its next turn. */
  const handOff = useCallback(
    (
      list: keyof PendingInputs,
      submission: ComposerSubmission,
      attachments?: AttachedFile[],
    ) => {
      const run = activeRunRef.current;
      const item = createPendingInput(submission, attachments);
      if (run?.kind !== "chat" || !item) return false;
      dispatch({ type: "add_pending", list, item });
      setSendError(null);
      track(
        list === "steers" ? deliverSteer(run.id, item) : deliverQueued(item),
      );
      return true;
    },
    [deliverQueued, deliverSteer, dispatch, track],
  );

  const steer = useCallback(
    (submission: ComposerSubmission, attachments?: AttachedFile[]) =>
      handOff("steers", submission, attachments),
    [handOff],
  );

  const queue = useCallback(
    (submission: ComposerSubmission, attachments?: AttachedFile[]) =>
      handOff("queue", submission, attachments),
    [handOff],
  );

  const removeQueued = useCallback(
    async (id: string) => {
      try {
        const res = await deleteQueued(activeSessionRef.current.id, id);
        // 404: delivered, or never reached the server queue.
        if (res.ok || res.status === 404) {
          dispatch({ type: "remove_pending", id });
        } else {
          setSendError(`Failed to remove queued message (${res.status})`);
        }
      } catch (e) {
        setSendError(getErrorMessage(e));
      }
    },
    [deleteQueued, dispatch],
  );

  /** Take a queued message back for the composer; null when it is already
   * on its way (delivered, or waiting to be sent when the run ends). */
  const takeBackQueued = useCallback(
    async (id: string): Promise<PendingInput | null> => {
      const item = chatStateRef.current.pending.queue.find(
        (queued) => queued.id === id,
      );
      if (!item) return null;
      try {
        const res = await deleteQueued(activeSessionRef.current.id, id);
        if (!res.ok || !isPending(id)) return null;
        dispatch({ type: "remove_pending", id });
        return item;
      } catch (e) {
        setSendError(getErrorMessage(e));
        return null;
      }
    },
    [deleteQueued, dispatch, isPending],
  );

  /** Move a queued message to the current turn. The server moves the message
   * it built, so a reloaded item keeps its attachments. */
  const steerQueued = useCallback(
    (id: string) => {
      const sessionId = activeSessionRef.current.id;
      track(
        (async () => {
          try {
            const res = await fetch(
              `/api/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(id)}/steer`,
              { method: "POST" },
            );
            if (!isPending(id)) return;
            if (res.ok) {
              dispatch({ type: "move_pending", id, to: "steers" });
            } else if (res.status !== 404 && res.status !== 409) {
              // 404: already on its way. 409: the turn is ending, so the item
              // stays queued and becomes the next turn.
              setSendError(`Failed to steer queued message (${res.status})`);
            }
          } catch (e) {
            setSendError(getErrorMessage(e));
          }
        })(),
      );
    },
    [dispatch, isPending, track],
  );

  const rewindAndSend = useCallback(
    async (rewindTo: number, input: string) => {
      const content = input.trim();
      if (!content || loading) return;
      await startTurn(
        { type: "rewind_and_start_turn", rewindTo, content },
        { message: content, rewind_to: rewindTo },
      );
    },
    [loading, startTurn],
  );

  const compactSession = useCallback(async () => {
    const session = activeSessionRef.current;
    if (loading) return false;
    if (session.isDraft) {
      setCompactError("nothing to compact");
      return false;
    }

    const sessionId = session.id;
    setCompactError(null);
    setSendError(null);
    setRunKind("compact");

    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/compact`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            provider: config.provider || undefined,
            model: config.model || undefined,
          }),
        },
      );

      const data = (await res.json()) as CompactResponse | ChatErrorResponse;
      if (activeSessionRef.current.id !== sessionId) return false;

      if (!res.ok) {
        const detail = getErrorDetail(data);
        const existingRun = getRunFromDetail(detail);
        if (res.status === 409 && existingRun?.id) {
          // Another client started a run; attach to it with its own kind.
          streamRun(existingRun, sessionId, existingRun.last_seq || 0);
          return false;
        }
        throw new Error(getMessageFromDetail(detail, "Compaction failed"));
      }

      fetchSessions();
      streamRun((data as CompactResponse).run, sessionId, 0);
      return true;
    } catch (e) {
      if (activeSessionRef.current.id === sessionId) {
        console.error("Failed to start compaction:", e);
        setRunKind(null);
        setCompactError(getErrorMessage(e));
      }
      return false;
    }
  }, [config.model, config.provider, fetchSessions, loading, streamRun]);

  const decidePermission = useCallback(
    async (decision: "allow" | "deny") => {
      const head = pendingPermissions[0];
      const runId = activeRunRef.current?.id;
      if (!head || !runId) return;

      // permission_resolved drives the clear; pre-clearing here would strand
      // the prompt if this POST fails while the server-side wait is still pending.
      try {
        const res = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/decide`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              request_id: head.request_id,
              decision,
            }),
          },
        );
        if (!res.ok) {
          console.error(`Decide POST failed: ${res.status}`);
        }
      } catch (e) {
        console.error("Failed to send decision:", e);
      }
    },
    [pendingPermissions],
  );

  const createSession = useCallback(() => {
    if (sessionLoading) return;

    stopStreaming();
    initRef.current = true;
    sessionRequestTokenRef.current += 1;
    const session = createDraftSession();

    setActiveSessionSnapshot(session);
    dispatch({ type: "set_messages", messages: [], sessionId: session.id });
    // Refresh from server to get accurate is_running, then prepend the new draft
    fetchSessions();
  }, [
    fetchSessions,
    sessionLoading,
    setActiveSessionSnapshot,
    stopStreaming,
    dispatch,
  ]);

  const selectSession = useCallback(
    async (sessionId: string) => {
      if (!sessionId || sessionId === activeSession.id) return;

      stopStreaming();
      initRef.current = true;
      const requestToken = sessionRequestTokenRef.current + 1;
      sessionRequestTokenRef.current = requestToken;
      setSessionLoading(true);

      const summary = sessions.find((session) => session.id === sessionId);
      if (summary) {
        setActiveSessionSnapshot(summary);
      }
      dispatch({ type: "set_messages", messages: [], sessionId });
      setPendingPermissions([]);

      const isStillCurrent = () =>
        isCurrentWorkspaceRequest({
          pendingRequestToken: sessionRequestTokenRef.current,
          requestToken,
          activeCwd: cwdRef.current,
          requestCwd: config.cwd,
        });

      try {
        await loadSession(sessionId, {
          requestCwd: config.cwd,
          requestToken,
        });
        fetchSessions();
      } catch (e) {
        console.error("Failed to load session:", e);
      } finally {
        if (isStillCurrent()) {
          setSessionLoading(false);
        }
      }
    },
    [
      activeSession.id,
      config.cwd,
      fetchSessions,
      loadSession,
      setActiveSessionSnapshot,
      sessions,
      stopStreaming,
      dispatch,
    ],
  );

  const deleteSession = useCallback(
    async (sessionId: string) => {
      if (!sessionId) return;

      const isDeletingActive = sessionId === activeSession.id;
      const deletedIndex = sessions.findIndex(
        (session) => session.id === sessionId,
      );
      const remainingSessions = sessions.filter(
        (session) => session.id !== sessionId,
      );
      const fallbackSession =
        deletedIndex >= 0
          ? remainingSessions[deletedIndex] ||
            remainingSessions[deletedIndex - 1] ||
            null
          : null;

      setSessionLoading(true);
      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}`,
          {
            method: "DELETE",
          },
        );
        if (!res.ok) throw new Error("Failed to delete session");

        if (!isDeletingActive) {
          setSessions(remainingSessions);
          return;
        }

        stopStreaming();
        initRef.current = true;
        sessionRequestTokenRef.current += 1;
        const requestToken = sessionRequestTokenRef.current;

        removeActiveSession(config.cwd);

        if (fallbackSession && !fallbackSession.isDraft) {
          setSessions(remainingSessions);
          setActiveSessionSnapshot(fallbackSession);
          dispatch({
            type: "set_messages",
            messages: [],
            sessionId: fallbackSession.id,
          });
          await loadSession(fallbackSession.id, {
            requestCwd: config.cwd,
            requestToken,
          });
          return;
        }

        const draft = createDraftSession();
        setActiveSessionSnapshot(draft);
        setSessions([draft]);
        dispatch({ type: "set_messages", messages: [], sessionId: draft.id });
        setRunKind(null);
      } catch (e) {
        console.error("Failed to delete session:", e);
      } finally {
        setSessionLoading(false);
      }
    },
    [
      activeSession.id,
      config.cwd,
      loadSession,
      sessions,
      setActiveSessionSnapshot,
      stopStreaming,
      dispatch,
    ],
  );

  // Single source of init: first mount and any cwd change reset state and
  // reload the workspace's sessions. `loadSession` is read through
  // `loadSessionRef` so this effect doesn't need to re-fire when its identity
  // changes (which would happen on every cwd change).
  useEffect(() => {
    const cwdChanged = cwdRef.current !== config.cwd;
    if (cwdChanged) {
      stopStreaming();
      cwdRef.current = config.cwd;
      initRef.current = false;
      setSessions([]);
      const draft = createDraftSession();
      setActiveSessionSnapshot(draft);
      dispatch({ type: "set_messages", messages: [], sessionId: draft.id });
    }
    if (initRef.current) return;
    initRef.current = true;

    const requestCwd = config.cwd;
    const requestToken = sessionRequestTokenRef.current + 1;
    sessionRequestTokenRef.current = requestToken;
    setSessionLoading(true);

    const isStillCurrent = () =>
      isCurrentWorkspaceRequest({
        pendingRequestToken: sessionRequestTokenRef.current,
        requestToken,
        activeCwd: cwdRef.current,
        requestCwd,
      });

    void (async () => {
      try {
        if (!isStillCurrent()) return;
        const preferredSessionId = loadActiveSession(requestCwd);
        const res = await fetch(
          `/api/sessions?cwd=${encodeURIComponent(requestCwd)}`,
        );
        if (!res.ok) throw new Error("Failed to load sessions");
        const data = (await res.json()) as SessionsResponse;
        if (!isStillCurrent()) return;

        const savedSessions = data.sessions || [];
        setSessions(savedSessions);
        const initialSessionId = resolveInitialSessionId(
          savedSessions,
          preferredSessionId,
        );

        if (initialSessionId) {
          const summary = savedSessions.find(
            (session) => session.id === initialSessionId,
          );
          if (summary) {
            setActiveSessionSnapshot(summary);
          }
          dispatch({
            type: "set_messages",
            messages: [],
            sessionId: initialSessionId,
          });
          await loadSessionRef.current?.(initialSessionId, {
            requestCwd,
            requestToken,
          });
        } else {
          const draft = createDraftSession();
          setActiveSessionSnapshot(draft);
          setSessions([draft]);
          dispatch({ type: "set_messages", messages: [], sessionId: draft.id });
          setRunKind(null);
        }
      } catch (e) {
        console.error("Failed to initialize sessions:", e);
      } finally {
        if (isStillCurrent()) setSessionLoading(false);
      }
    })();
  }, [config.cwd, setActiveSessionSnapshot, stopStreaming, dispatch]);

  useEffect(() => {
    return () => {
      stopStreaming();
    };
  }, [stopStreaming]);

  const messages = useMemo(
    () => buildRenderMessages(chatState.rawMessages, chatState.toolRuntimeById),
    [chatState.rawMessages, chatState.toolRuntimeById],
  );

  // Current context occupancy from the latest turn with usage. A compact
  // marker stops the scan because pre-compact occupancy is no longer current.
  const currentContext = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const message = messages[i];
      if (!message || isCompactMarker(message)) break;
      const stats = message.role === "assistant" ? message.stats : undefined;
      if (!stats) continue;
      if (
        stats.context_tokens !== undefined &&
        stats.context_window !== undefined
      ) {
        return { tokens: stats.context_tokens, window: stats.context_window };
      }
      break;
    }
    return null;
  }, [messages]);

  return {
    messages,
    messageSessionId: chatState.messageSessionId,
    sessionUsage: chatState.sessionUsage,
    currentContext,
    loading,
    runKind,
    compactError,
    sendError,
    sessions,
    activeSession,
    sessionLoading,
    pendingPermission: pendingPermissions[0] ?? null,
    pending: chatState.pending,
    send,
    steer,
    queue,
    removeQueued,
    steerQueued,
    takeBackQueued,
    rewindAndSend,
    compactSession,
    cancel,
    decidePermission,
    createSession,
    selectSession,
    deleteSession,
  };
}
