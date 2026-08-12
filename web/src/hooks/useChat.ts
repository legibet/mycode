/**
 * Chat state management hook.
 * Keeps large document payloads out of React state; the request body still sends them.
 */

import {
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
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
  PermissionRequest,
  RemoteConfig,
  RunInfo,
  RunKind,
  SessionResponse,
  SessionSummary,
  SessionsResponse,
  StreamEvent,
  ToolRuntime,
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
  /** Session cumulative cost estimate; null when unknown. Set on session
   * load, updated live by SSE usage events. */
  sessionCost: number | null;
  /** Snapshot of rawMessages taken before the latest optimistic turn.
   * Used by 'rollback' to restore state when the request fails. */
  preTurnRawMessages: ChatMessage[] | null;
}

type ChatAction =
  | {
      type: "set_messages";
      messages: ChatMessage[];
      sessionId?: string | null;
      sessionCost?: number | null;
      replayEvents?: StreamEvent[];
      expectedSessionId?: string | null;
    }
  | {
      type: "start_turn";
      content: string;
      attachments?: AttachedFile[];
      workspaceFiles?: WorkspaceFileReference[];
    }
  | { type: "rewind_and_start_turn"; rewindTo: number; content: string }
  | { type: "apply_event"; event: StreamEvent }
  | { type: "rollback" };

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
        sessionCost: action.sessionCost ?? null,
        preTurnRawMessages: null,
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
        rawMessages: snapshot,
        messageSessionId: state.messageSessionId,
        toolRuntimeById: {},
        sessionCost: state.sessionCost,
        preTurnRawMessages: null,
      };
    }

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
        rawMessages = appendAssistantDelta(
          rawMessages,
          "text",
          `\n\n**Error:** ${event.message || "Unknown"}`,
        );
      } else if (event.type === "usage") {
        // The event carries the turn's cumulative values, so replace the
        // previous snapshot instead of summing it again.
        const patch: Partial<MessageMeta> = {
          context_tokens: event.context_tokens ?? null,
          turn_usage: event.turn_usage ?? {},
          turn_cost: event.turn_cost ?? null,
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
          sessionCost: event.session_cost ?? null,
        };
      } else if (event.type === "compact") {
        rawMessages = [...rawMessages, { role: "compact", content: [] }];
      }

      return { ...state, rawMessages, toolRuntimeById };
    }
    default:
      return state;
  }
}

export function useChat(
  config: LocalConfig,
  remoteConfig?: RemoteConfig | null,
) {
  const [chatState, dispatch] = useReducer(chatReducer, {
    messageSessionId: null,
    rawMessages: [],
    toolRuntimeById: {},
    sessionCost: null,
    preTurnRawMessages: null,
  });
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSession, setActiveSession] = useState(createDraftSession);
  // Kind of the run this client is following; null when idle. `loading` is
  // derived so chat and compact runs share the busy/cancel plumbing.
  const [runKind, setRunKind] = useState<RunKind | null>(null);
  const [compactError, setCompactError] = useState<string | null>(null);
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
  const sessionRequestTokenRef = useRef(0);
  const streamAbortRef = useRef<AbortController | null>(null);
  const streamTokenRef = useRef(0);
  const activeRunRef = useRef<RunInfo | null>(null);
  const loadSessionRef = useRef<
    | ((
        sessionId: string,
        options?: { requestCwd?: string; requestToken?: number },
      ) => Promise<SessionResponse | null>)
    | null
  >(null);

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
    setPendingPermissions([]);
  }, []);

  const streamRun = useCallback(
    async (run: RunInfo, sessionId: string, after = 0): Promise<void> => {
      const runId = run?.id;
      if (!runId) return;

      streamTokenRef.current += 1;
      const token = streamTokenRef.current;
      streamAbortRef.current?.abort();

      const controller = new AbortController();
      streamAbortRef.current = controller;
      activeRunRef.current = run;
      const kind = run.kind;
      setRunKind(kind);

      const recoverSession = async () => {
        if (
          streamTokenRef.current !== token ||
          activeSessionRef.current.id !== sessionId
        ) {
          return true;
        }

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
        let sawDone = false;

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
              if (
                streamTokenRef.current !== token ||
                activeSessionRef.current.id !== sessionId
              ) {
                continue;
              }
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
          if (
            !recovered &&
            streamTokenRef.current === token &&
            activeSessionRef.current.id === sessionId
          ) {
            const message =
              "Stream disconnected. Reload the session to resume.";
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
            setRunKind(null);
          }

          fetchSessions();
        }
      }
    },
    [fetchSessions],
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
          sessionCost: data.session_cost ?? null,
          replayEvents,
          expectedSessionId: data.session?.id ?? sessionId,
        });
      });
      if (replayedPermissions.size) {
        setPendingPermissions(Array.from(replayedPermissions.values()));
      }

      activeRunRef.current = run;

      if (run?.id) {
        const lastSeq = pendingEvents.at(-1)?.seq ?? 0;
        streamRun(run, data.session.id, lastSeq);
      } else {
        setRunKind(null);
      }

      return data;
    },
    [config.cwd, setActiveSessionSnapshot, streamRun],
  );

  useEffect(() => {
    loadSessionRef.current = loadSession;
  }, [loadSession]);

  const send = useCallback(
    async (submission: ComposerSubmission, attachments?: AttachedFile[]) => {
      const content = submission.text.trim();
      const workspaceFiles = dedupeWorkspaceFiles(submission.workspaceFiles);
      if (
        (!content && !attachments?.length && !workspaceFiles.length) ||
        loading
      )
        return false;

      const sessionId = activeSession.id;
      const requestCwd = config.cwd;
      const requestToken = requestTokenRef.current + 1;

      requestTokenRef.current = requestToken;
      pendingRequestTokenRef.current = requestToken;

      dispatch({
        type: "start_turn",
        content,
        ...(attachments?.length ? { attachments } : {}),
        ...(workspaceFiles.length ? { workspaceFiles } : {}),
      });
      setRunKind("chat");
      setCompactError(null);

      const commonFields = {
        session_id: sessionId,
        provider: config.provider || undefined,
        model: config.model || undefined,
        cwd: config.cwd,
        reasoning_effort: getReasoningEffortOverride(config, remoteConfig),
      };

      // Use structured `input` blocks when any attachment is present.
      const body =
        attachments?.length || workspaceFiles.length
          ? {
              ...commonFields,
              input: [
                ...(content ? [{ type: "text", text: content }] : []),
                ...workspaceFiles.map(workspaceRefToInputBlock),
                ...(attachments ?? []).map(attachmentToInputBlock),
              ],
            }
          : { ...commonFields, message: content };

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
          const detail = getErrorDetail(data);
          const existingRun = getRunFromDetail(detail);
          if (res.status === 409 && existingRun?.id) {
            if (isCurrentRequest) {
              pendingRequestTokenRef.current = 0;
              dispatch({ type: "rollback" });
              streamRun(existingRun, sessionId, existingRun.last_seq || 0);
            }
            return false;
          }
          throw new Error(getMessageFromDetail(detail, "Failed to start task"));
        }

        pendingRequestTokenRef.current = 0;

        if (!isCurrentRequest) {
          return false;
        }

        const chatData = data as ChatResponse;

        // Update active session from backend response (has real title, id, etc.)
        if (chatData.session) {
          const session = chatData.session;
          setActiveSessionSnapshot(session);
          saveActiveSession(requestCwd, session.id);
        }

        // Refresh sidebar immediately so title + is_running are visible
        fetchSessions();

        streamRun(chatData.run, sessionId, 0);
        return true;
      } catch (e) {
        if (
          pendingRequestTokenRef.current === requestToken &&
          activeSessionRef.current.id === sessionId
        ) {
          pendingRequestTokenRef.current = 0;
          setRunKind(null);
          dispatch({ type: "rollback" });
          dispatch({
            type: "apply_event",
            event: { type: "error", message: getErrorMessage(e) },
          });
        }
        return false;
      }
    },
    [
      activeSession.id,
      config,
      fetchSessions,
      loading,
      remoteConfig,
      setActiveSessionSnapshot,
      streamRun,
    ],
  );

  const rewindAndSend = useCallback(
    async (rewindTo: number, input: string) => {
      const content = input.trim();
      if (!content || loading) return;

      const sessionId = activeSession.id;
      const requestCwd = config.cwd;
      const requestToken = requestTokenRef.current + 1;

      requestTokenRef.current = requestToken;
      pendingRequestTokenRef.current = requestToken;

      dispatch({ type: "rewind_and_start_turn", rewindTo, content });
      setRunKind("chat");
      setCompactError(null);

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            message: content,
            rewind_to: rewindTo,
            provider: config.provider || undefined,
            model: config.model || undefined,
            cwd: config.cwd,
            reasoning_effort: getReasoningEffortOverride(config, remoteConfig),
          }),
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
          // Restore original messages on failure (rewind was optimistic).
          if (isCurrentRequest) {
            pendingRequestTokenRef.current = 0;
            dispatch({ type: "rollback" });

            // If 409 (active run), attach to the existing run.
            const detail = getErrorDetail(data);
            const existingRun = getRunFromDetail(detail);
            if (res.status === 409 && existingRun?.id) {
              streamRun(existingRun, sessionId, existingRun.last_seq || 0);
              return;
            }

            setRunKind(null);
            dispatch({
              type: "apply_event",
              event: {
                type: "error",
                message: getMessageFromDetail(detail, "Failed to start task"),
              },
            });
          }
          return;
        }

        pendingRequestTokenRef.current = 0;

        if (!isCurrentRequest) return;

        const chatData = data as ChatResponse;

        if (chatData.session) {
          const session = chatData.session;
          setActiveSessionSnapshot(session);
          saveActiveSession(requestCwd, session.id);
        }

        fetchSessions();
        streamRun(chatData.run, sessionId, 0);
      } catch (e) {
        if (
          pendingRequestTokenRef.current === requestToken &&
          activeSessionRef.current.id === sessionId
        ) {
          pendingRequestTokenRef.current = 0;
          dispatch({ type: "rollback" });
          setRunKind(null);
          dispatch({
            type: "apply_event",
            event: { type: "error", message: getErrorMessage(e) },
          });
        }
      }
    },
    [
      activeSession.id,
      config,
      fetchSessions,
      loading,
      remoteConfig,
      setActiveSessionSnapshot,
      streamRun,
    ],
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

  const cancel = useCallback(() => {
    const runId = activeRunRef.current?.id;
    const sessionId = activeSessionRef.current.id;

    streamTokenRef.current += 1;
    pendingRequestTokenRef.current = 0;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeRunRef.current = null;
    setPendingPermissions([]);

    if (!runId) {
      setRunKind(null);
      return;
    }

    void (async () => {
      await cancelRun(runId);
      if (activeSessionRef.current.id !== sessionId) return;

      try {
        await loadSessionRef.current?.(sessionId);
        fetchSessions();
      } catch (error) {
        console.error("Failed to reload session after cancel:", error);
        if (activeSessionRef.current.id === sessionId) {
          setRunKind(null);
        }
      }
    })();
  }, [cancelRun, fetchSessions]);

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
  }, [fetchSessions, sessionLoading, setActiveSessionSnapshot, stopStreaming]);

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
    ],
  );

  const clearSession = useCallback(async () => {
    if (loading || sessionLoading) return;

    const session = activeSessionRef.current;
    if (session.isDraft) return;

    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(session.id)}/clear`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(`Clear failed with status ${res.status}`);
    } catch (e) {
      console.error("Failed to clear session:", e);
      return;
    }

    if (activeSessionRef.current.id === session.id) {
      dispatch({ type: "set_messages", messages: [], sessionId: session.id });
    }
    fetchSessions();
  }, [loading, sessionLoading, fetchSessions]);

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
  }, [config.cwd, setActiveSessionSnapshot, stopStreaming]);

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
    sessionCost: chatState.sessionCost,
    currentContext,
    loading,
    runKind,
    compactError,
    sessions,
    activeSession,
    sessionLoading,
    pendingPermission: pendingPermissions[0] ?? null,
    send,
    rewindAndSend,
    compactSession,
    cancel,
    decidePermission,
    createSession,
    selectSession,
    deleteSession,
    clearSession,
  };
}
