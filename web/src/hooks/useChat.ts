/**
 * Sessions and runs for the chat UI: session loading, start requests, the
 * run stream, steer and queue, and Stop. Message state lives in chatState.
 */

import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  ChatResponse,
  RemoteConfig,
  RunInfo,
  RunKind,
  RunResponse,
  ServerEvent,
  SessionResponse,
  SessionsResponse,
} from "@/api";
import { fetchJson, readApiError, readSse, SSE_DONE } from "@/api";
import {
  buildInputBlocks,
  type ChatAction,
  chatReducer,
  createDraftSession,
  createPendingInput,
  dedupeWorkspaceFiles,
  INITIAL_CHAT_STATE,
  pendingFromMessage,
  resolveInitialSession,
  routeRunEvents,
} from "@/hooks/chatState";
import type {
  AttachedFile,
  ComposerSubmission,
  LocalConfig,
  PendingInput,
  PendingInputs,
  SessionSummary,
} from "@/types";
import { getReasoningEffortOverride } from "@/utils/config";
import { buildRenderMessages, currentContext, readUsageTotals } from "@/utils/messages";
import { loadActiveSession, removeActiveSession, saveActiveSession } from "@/utils/storage";

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

function postPendingInput(url: string, item: PendingInput) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input: item.input, input_id: item.id }),
  });
}

function deleteQueued(sessionId: string, id: string) {
  return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
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
  // Set once the init effect has run for the current cwd, so a rerun
  // without a cwd change (StrictMode's double mount) does not load twice.
  const initRef = useRef(false);
  const cwdRef = useRef(config.cwd);
  const activeSessionRef = useRef(activeSession);
  /** Last token handed to a start request. */
  const requestTokenRef = useRef(0);
  /** Token of the start request in flight, `POST /api/chat` or
   * `POST .../compact`; 0 when none. A session switch or a newer request
   * takes over, and the older response is abandoned. */
  const startRequestTokenRef = useRef(0);
  /** Token of a start request in flight at Stop; the run its response names
   * is stopped as soon as it arrives. */
  const stoppedRequestTokenRef = useRef(0);
  const sessionRequestTokenRef = useRef(0);
  const streamAbortRef = useRef<AbortController | null>(null);
  const streamTokenRef = useRef(0);
  const activeRunRef = useRef<RunInfo | null>(null);
  const onRestoreRef = useRef(onRestore);
  const sendPendingRef = useRef<typeof sendPending | null>(null);
  /** Steer and queue requests in flight. A stream end waits for them before
   * deciding what is still pending. */
  const inflightRef = useRef(new Set<Promise<void>>());
  const loadSessionRef = useRef<typeof loadSession | null>(null);
  /** Set when the shown session must be reloaded once this client is quiet:
   * a run started there while a stream or a start request was in flight, or
   * the event stream reconnected. Holds the run id, or `"*"` for a reconnect. */
  const resyncRef = useRef<string | null>(null);

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

  const setActiveSessionSummary = useCallback((session: SessionSummary) => {
    activeSessionRef.current = session;
    setActiveSession(session);
  }, []);

  /** Show a new draft session with empty history. */
  const showDraft = useCallback(() => {
    const draft = createDraftSession();
    setActiveSessionSummary(draft);
    dispatch({ type: "set_messages", messages: [], sessionId: draft.id });
    return draft;
  }, [dispatch, setActiveSessionSummary]);

  const fetchSessions = useCallback(async (): Promise<SessionSummary[]> => {
    const requestCwd = config.cwd;
    if (requestCwd !== cwdRef.current) return [];

    try {
      const data = await fetchJson<SessionsResponse>(
        `/api/sessions?cwd=${encodeURIComponent(requestCwd)}`,
      );
      if (requestCwd !== cwdRef.current) {
        return [];
      }
      const savedSessions = data.sessions;
      const active = activeSessionRef.current;
      const sessionsWithDraft =
        active.isDraft && !savedSessions.some((session) => session.id === active.id)
          ? [active, ...savedSessions]
          : savedSessions;

      setSessions(sessionsWithDraft);

      const syncedActive = sessionsWithDraft.find((session) => session.id === active.id);
      if (syncedActive && syncedActive !== active) {
        setActiveSessionSummary(syncedActive);
      }

      return sessionsWithDraft;
    } catch (e) {
      console.error("Failed to load sessions:", e);
      return [];
    }
  }, [config.cwd, setActiveSessionSummary]);

  const stopStreaming = useCallback(() => {
    streamTokenRef.current += 1;
    startRequestTokenRef.current = 0;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeRunRef.current = null;
    setRunKind(null);
    setCompactError(null);
    setSendError(null);
  }, []);

  /** Whether a start request's response may still change the UI. Session and
   * workspace switches clear the start request token, except the initial
   * session load, which only changes the shown session. */
  const isCurrentStartRequest = useCallback(
    (token: number, sessionId: string) =>
      startRequestTokenRef.current === token && activeSessionRef.current.id === sessionId,
    [],
  );

  /** Reload the shown session for a run announced while this client was
   * busy, once nothing of its own is in flight. */
  const resyncIfQuiet = useCallback((sessionId: string) => {
    if (!resyncRef.current || streamAbortRef.current || startRequestTokenRef.current) return;
    if (activeSessionRef.current.id !== sessionId) return;
    resyncRef.current = null;
    void loadSessionRef.current?.(sessionId).catch((error) => {
      console.error("Failed to sync session:", error);
    });
  }, []);

  const streamRun = useCallback(
    async (run: RunInfo, sessionId: string, after = 0, interrupted = false): Promise<void> => {
      // Stopped or failed runs return pending input to the composer; a run
      // that ended normally sends it as the next turn.
      let stopped = interrupted;
      let sawDone = false;

      const token = ++streamTokenRef.current;
      streamAbortRef.current?.abort();
      // The run this client was told about is the one it now follows.
      if (resyncRef.current === run.id) resyncRef.current = null;

      const controller = new AbortController();
      streamAbortRef.current = controller;
      activeRunRef.current = run;
      const kind = run.kind;
      setRunKind(kind);
      // Whether this stream still drives the UI: no newer stream, same session.
      const isCurrent = () =>
        streamTokenRef.current === token && activeSessionRef.current.id === sessionId;

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

      // The stream ended early and the session could not be reloaded.
      const markDisconnected = () => {
        if (!isCurrent()) return;
        const message = "Stream disconnected. Reload the session to resume.";
        stopped = true;
        if (kind === "compact") {
          setCompactError(message);
        } else {
          dispatch({ type: "apply_event", event: { type: "error", message } });
        }
      };

      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(run.id)}/stream?after=${after}`, {
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);

        for await (const event of readSse(res)) {
          if (event === SSE_DONE) {
            sawDone = true;
            continue;
          }
          if (!isCurrent()) continue;
          const routed = routeRunEvents(kind, [event]);
          if (routed.stopped) stopped = true;
          if (routed.compactError !== null) {
            console.error("Compaction failed:", routed.compactError);
            setCompactError(routed.compactError);
          }
          for (const applied of routed.apply) {
            dispatch({ type: "apply_event", event: applied });
          }
        }

        if (kind === "compact" && sawDone) {
          await recoverSession();
        }

        if (!sawDone) {
          if (await recoverSession()) return;
          markDisconnected();
        }
      } catch (e) {
        if (!(e instanceof Error) || e.name !== "AbortError") {
          if (!(await recoverSession())) markDisconnected();
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

          void fetchSessions();
          resyncIfQuiet(sessionId);
        }
      }
    },
    [fetchSessions, resyncIfQuiet, restorePending, takePending, dispatch],
  );

  const loadSession = useCallback(
    async (
      sessionId: string,
      options?: { requestCwd?: string; requestToken?: number },
    ): Promise<SessionResponse | null> => {
      const requestCwd = options?.requestCwd ?? config.cwd;
      const requestToken = options?.requestToken ?? sessionRequestTokenRef.current;
      const isStillCurrent = () =>
        sessionRequestTokenRef.current === requestToken && cwdRef.current === requestCwd;

      if (!isStillCurrent()) return null;

      const data = await fetchJson<SessionResponse>(
        `/api/sessions/${encodeURIComponent(sessionId)}`,
      );
      const session = data.session;
      if (!isStillCurrent() || !session) return null;
      resyncRef.current = null;

      setActiveSessionSummary(session);
      saveActiveSession(requestCwd, session.id);
      const run = data.active_run;

      // Replayed events take the live stream's route.
      const replay = routeRunEvents(run?.kind ?? "chat", data.pending_events);
      if (replay.compactError !== null) setCompactError(replay.compactError);
      startTransition(() => {
        dispatch({
          type: "set_messages",
          messages: data.messages,
          sessionId: session.id,
          sessionUsage: readUsageTotals(data.session_usage, data.session_cost),
          replayEvents: replay.apply,
          expectedSessionId: session.id,
          pending: {
            steers: data.pending.steers.map(pendingFromMessage),
            queue: data.pending.queue.map(pendingFromMessage),
          },
          jobs: data.jobs,
        });
      });

      activeRunRef.current = run;

      if (run) {
        // The history covers every event up to the run's last seq when none is pending.
        const lastSeq = data.pending_events.at(-1)?.seq ?? run.last_seq;
        void streamRun(run, session.id, lastSeq, replay.stopped);
      } else {
        setRunKind(null);
      }

      return data;
    },
    [config.cwd, dispatch, setActiveSessionSummary, streamRun],
  );

  useEffect(() => {
    loadSessionRef.current = loadSession;
  }, [loadSession]);

  const cancel = useCallback(() => {
    const runId = activeRunRef.current?.id;
    if (!runId && startRequestTokenRef.current) {
      // The run is unknown until the start request returns; it is stopped then.
      stoppedRequestTokenRef.current = startRequestTokenRef.current;
      return;
    }
    const sessionId = activeSessionRef.current.id;

    streamTokenRef.current += 1;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeRunRef.current = null;
    dispatch({ type: "clear_permissions" });
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
      try {
        await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
      } catch (e) {
        console.error("Failed to cancel:", e);
      }
      if (activeSessionRef.current.id !== sessionId) return;

      const delivered = new Set<string>();
      try {
        const data = await loadSessionRef.current?.(sessionId);
        for (const message of data?.messages ?? []) {
          for (const id of message.meta?.input_ids ?? []) delivered.add(id);
        }
        void fetchSessions();
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
  }, [dispatch, fetchSessions, restorePending, takePending]);

  /** Optimistic turn plus `POST /api/chat` with the current model settings.
   * @param pendingItems Pending items this request sends; a rejection returns
   *   them to the composer instead of attaching to another run. */
  const startTurn = useCallback(
    async (
      turn: Extract<ChatAction, { type: "start_turn" | "rewind_and_start_turn" }>,
      request: { message: string; rewind_to?: number } | { input: Record<string, unknown>[] },
      pendingItems: PendingInput[] = [],
    ): Promise<boolean> => {
      const sessionId = activeSessionRef.current.id;
      const requestToken = ++requestTokenRef.current;
      startRequestTokenRef.current = requestToken;

      dispatch(turn);
      setRunKind("chat");
      setCompactError(null);
      setSendError(null);

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            provider: config.provider || undefined,
            model: config.model || undefined,
            cwd: config.cwd,
            reasoning_effort: getReasoningEffortOverride(config, remoteConfig),
            ...request,
          }),
        });

        if (!res.ok) {
          const error = await readApiError(res, "Failed to start task");
          // Restore original messages on failure (the turn was optimistic).
          if (isCurrentStartRequest(requestToken, sessionId)) {
            startRequestTokenRef.current = 0;
            dispatch({ type: "rollback" });

            if (res.status === 409 && error.run && pendingItems.length === 0) {
              void streamRun(error.run, sessionId, error.run.last_seq);
              if (stoppedRequestTokenRef.current === requestToken) cancel();
              return false;
            }

            restorePending(pendingItems);
            setRunKind(null);
            setSendError(error.message);
            resyncIfQuiet(sessionId);
          }
          return false;
        }

        const data = (await res.json()) as ChatResponse;
        if (!isCurrentStartRequest(requestToken, sessionId)) return false;
        startRequestTokenRef.current = 0;

        dispatch({ type: "accept_turn", message: data.message });

        // Update active session from backend response (has real title, id, etc.)
        setActiveSessionSummary(data.session);
        saveActiveSession(config.cwd, data.session.id);

        // Refresh sidebar immediately so title + is_running are visible
        void fetchSessions();
        void streamRun(data.run, sessionId, 0);
        if (stoppedRequestTokenRef.current === requestToken) cancel();
        return true;
      } catch (e) {
        if (isCurrentStartRequest(requestToken, sessionId)) {
          startRequestTokenRef.current = 0;
          setRunKind(null);
          dispatch({ type: "rollback" });
          restorePending(pendingItems);
          setSendError(getErrorMessage(e));
          resyncIfQuiet(sessionId);
        }
        return false;
      }
    },
    [
      cancel,
      config,
      dispatch,
      fetchSessions,
      isCurrentStartRequest,
      remoteConfig,
      restorePending,
      resyncIfQuiet,
      setActiveSessionSummary,
      streamRun,
    ],
  );

  const send = useCallback(
    async (submission: ComposerSubmission, attachments: AttachedFile[] = []) => {
      const content = submission.text.trim();
      const workspaceFiles = dedupeWorkspaceFiles(submission.workspaceFiles);
      if ((!content && !attachments.length && !workspaceFiles.length) || loading) return false;

      // Use structured `input` blocks when any attachment is present.
      const request =
        attachments.length || workspaceFiles.length
          ? { input: buildInputBlocks(content, workspaceFiles, attachments) }
          : { message: content };
      return startTurn({ type: "start_turn", content, attachments, workspaceFiles }, request);
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
          attachments,
          workspaceFiles,
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
        failPending(item, (await readApiError(res, "Failed to queue")).message);
      } catch (e) {
        failPending(item, getErrorMessage(e));
      }
    },
    [failPending, isPending],
  );

  const deliverSteer = useCallback(
    async (runId: string, item: PendingInput) => {
      try {
        const res = await postPendingInput(`/api/runs/${encodeURIComponent(runId)}/steer`, item);
        if (!isPending(item.id) || res.ok) return;
        if (res.status === 409) {
          // The turn is ending: the item becomes the next turn instead.
          dispatch({ type: "move_pending", id: item.id, to: "queue" });
          await deliverQueued(item);
          return;
        }
        failPending(item, (await readApiError(res, "Failed to steer")).message);
      } catch (e) {
        failPending(item, getErrorMessage(e));
      }
    },
    [deliverQueued, dispatch, failPending, isPending],
  );

  /** Hand a message to the running chat: as a steer for its next step
   * boundary, or queued as its next turn. */
  const handOff = useCallback(
    (list: keyof PendingInputs, submission: ComposerSubmission, attachments?: AttachedFile[]) => {
      const run = activeRunRef.current;
      const item = createPendingInput(submission, attachments);
      if (run?.kind !== "chat" || !item) return false;
      dispatch({ type: "add_pending", list, item });
      setSendError(null);
      track(list === "steers" ? deliverSteer(run.id, item) : deliverQueued(item));
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
    [dispatch],
  );

  /** Take a queued message back for the composer; null when it is already
   * on its way (delivered, or waiting to be sent when the run ends). */
  const takeBackQueued = useCallback(
    async (id: string): Promise<PendingInput | null> => {
      const item = chatStateRef.current.pending.queue.find((queued) => queued.id === id);
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
    [dispatch, isPending],
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
    const requestToken = ++requestTokenRef.current;
    startRequestTokenRef.current = requestToken;
    setCompactError(null);
    setSendError(null);
    setRunKind("compact");
    const isCurrentRequest = () => isCurrentStartRequest(requestToken, sessionId);

    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: config.provider || undefined,
          model: config.model || undefined,
        }),
      });

      if (!res.ok) {
        const error = await readApiError(res, "Compaction failed");
        if (isCurrentRequest() && res.status === 409 && error.run) {
          // Another client started a run; attach to it with its own kind.
          startRequestTokenRef.current = 0;
          void streamRun(error.run, sessionId, error.run.last_seq);
          if (stoppedRequestTokenRef.current === requestToken) cancel();
          return false;
        }
        throw error;
      }

      const data = (await res.json()) as RunResponse;
      if (!isCurrentRequest()) return false;
      startRequestTokenRef.current = 0;
      void fetchSessions();
      void streamRun(data.run, sessionId, 0);
      if (stoppedRequestTokenRef.current === requestToken) cancel();
      return true;
    } catch (e) {
      if (isCurrentRequest()) {
        startRequestTokenRef.current = 0;
        console.error("Failed to start compaction:", e);
        setRunKind(null);
        setCompactError(getErrorMessage(e));
        resyncIfQuiet(sessionId);
      }
      return false;
    }
  }, [
    cancel,
    config.model,
    config.provider,
    fetchSessions,
    isCurrentStartRequest,
    loading,
    resyncIfQuiet,
    streamRun,
  ]);

  const pendingPermission = chatState.permissions[0] ?? null;

  const decidePermission = useCallback(
    async (decision: "allow" | "deny") => {
      const runId = activeRunRef.current?.id;
      if (!pendingPermission || !runId) return;

      // permission_resolved drives the clear; pre-clearing here would strand
      // the prompt if this POST fails while the server-side wait is still pending.
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/decide`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            request_id: pendingPermission.request_id,
            decision,
          }),
        });
        if (!res.ok) {
          console.error(`Decide POST failed: ${res.status}`);
        }
      } catch (e) {
        console.error("Failed to send decision:", e);
      }
    },
    [pendingPermission],
  );

  const createSession = useCallback(() => {
    if (sessionLoading) return;

    stopStreaming();
    sessionRequestTokenRef.current += 1;
    showDraft();
    // Refresh from server to get accurate is_running, then prepend the new draft
    void fetchSessions();
  }, [fetchSessions, sessionLoading, showDraft, stopStreaming]);

  const selectSession = useCallback(
    async (sessionId: string) => {
      if (sessionId === activeSession.id) return;

      stopStreaming();
      const requestToken = ++sessionRequestTokenRef.current;
      const requestCwd = config.cwd;
      setSessionLoading(true);

      const summary = sessions.find((session) => session.id === sessionId);
      if (summary) {
        setActiveSessionSummary(summary);
      }
      dispatch({ type: "set_messages", messages: [], sessionId });

      try {
        await loadSession(sessionId, { requestCwd, requestToken });
        void fetchSessions();
      } catch (e) {
        console.error("Failed to load session:", e);
      } finally {
        if (sessionRequestTokenRef.current === requestToken && cwdRef.current === requestCwd) {
          setSessionLoading(false);
        }
      }
    },
    [
      activeSession.id,
      config.cwd,
      fetchSessions,
      loadSession,
      setActiveSessionSummary,
      sessions,
      stopStreaming,
      dispatch,
    ],
  );

  const deleteSession = useCallback(
    async (sessionId: string) => {
      const isDeletingActive = sessionId === activeSession.id;
      const deletedIndex = sessions.findIndex((session) => session.id === sessionId);
      const remainingSessions = sessions.filter((session) => session.id !== sessionId);
      const fallbackSession =
        deletedIndex >= 0
          ? remainingSessions[deletedIndex] || remainingSessions[deletedIndex - 1] || null
          : null;

      setSessionLoading(true);
      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
          method: "DELETE",
        });
        if (!res.ok) throw new Error("Failed to delete session");

        if (!isDeletingActive) {
          setSessions(remainingSessions);
          return;
        }

        stopStreaming();
        const requestToken = ++sessionRequestTokenRef.current;

        removeActiveSession(config.cwd);

        if (fallbackSession && !fallbackSession.isDraft) {
          setSessions(remainingSessions);
          setActiveSessionSummary(fallbackSession);
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

        setSessions([showDraft()]);
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
      setActiveSessionSummary,
      showDraft,
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
      setSessions([]);
      showDraft();
    } else if (initRef.current) {
      return;
    }
    initRef.current = true;

    const requestCwd = config.cwd;
    const requestToken = ++sessionRequestTokenRef.current;
    setSessionLoading(true);

    const isStillCurrent = () =>
      sessionRequestTokenRef.current === requestToken && cwdRef.current === requestCwd;

    void (async () => {
      try {
        const preferredSessionId = loadActiveSession(requestCwd);
        const data = await fetchJson<SessionsResponse>(
          `/api/sessions?cwd=${encodeURIComponent(requestCwd)}`,
        );
        if (!isStillCurrent()) return;

        const savedSessions = data.sessions;
        setSessions(savedSessions);
        const initialSession = resolveInitialSession(savedSessions, preferredSessionId);

        if (initialSession) {
          setActiveSessionSummary(initialSession);
          dispatch({
            type: "set_messages",
            messages: [],
            sessionId: initialSession.id,
          });
          await loadSessionRef.current?.(initialSession.id, {
            requestCwd,
            requestToken,
          });
        } else {
          setSessions([showDraft()]);
          setRunKind(null);
        }
      } catch (e) {
        console.error("Failed to initialize sessions:", e);
      } finally {
        if (isStillCurrent()) setSessionLoading(false);
      }
    })();
  }, [config.cwd, setActiveSessionSummary, showDraft, stopStreaming, dispatch]);

  useEffect(() => {
    return () => {
      stopStreaming();
    };
  }, [stopStreaming]);

  // Runs this client did not start, such as a session woken by a finished
  // background command, are announced on `/api/events`. The shown session is
  // reloaded to attach to them: at once when idle, otherwise when the current
  // stream ends. There is no replay, so a reconnect reloads as well.
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events");
    let connected = false;

    const sync = (runId: string) => {
      const session = activeSessionRef.current;
      if (session.isDraft) return;
      if (streamAbortRef.current || startRequestTokenRef.current) {
        resyncRef.current = runId;
        return;
      }
      void loadSessionRef.current?.(session.id).catch((error) => {
        console.error("Failed to sync session:", error);
      });
    };

    source.onopen = () => {
      // The initial load covers the first connection; later opens are reconnects.
      if (connected) sync("*");
      connected = true;
    };
    source.onmessage = (message: MessageEvent<string>) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(message.data) as ServerEvent;
      } catch {
        return;
      }
      void fetchSessions();
      if (event.session_id !== activeSessionRef.current.id) return;
      if (event.type === "job_started") {
        dispatch({ type: "job_started", job: event.job });
      } else if (event.type === "job_finished") {
        dispatch({ type: "job_finished", toolUseId: event.job.tool_use_id });
      } else if (event.type === "run_started" && event.run.id !== activeRunRef.current?.id) {
        sync(event.run.id);
      }
    };
    return () => source.close();
  }, [dispatch, fetchSessions]);

  /** Kill a running background command. Its result still reaches the model;
   * the job leaves the list with the `job_finished` event. */
  const stopJob = useCallback(async (toolUseId: string) => {
    const sessionId = activeSessionRef.current.id;
    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/jobs/${encodeURIComponent(toolUseId)}`,
        { method: "DELETE" },
      );
      // 404: already finished.
      if (!res.ok && res.status !== 404) {
        setSendError(`Failed to stop background command (${res.status})`);
      }
    } catch (e) {
      setSendError(getErrorMessage(e));
    }
  }, []);

  const runningJobIds = useMemo(
    () => new Set(chatState.jobs.map((job) => job.tool_use_id)),
    [chatState.jobs],
  );
  const messages = useMemo(
    () => buildRenderMessages(chatState.rawMessages, chatState.toolRuntimeById, runningJobIds),
    [chatState.rawMessages, chatState.toolRuntimeById, runningJobIds],
  );
  const context = useMemo(() => currentContext(messages), [messages]);

  return {
    messages,
    messageSessionId: chatState.messageSessionId,
    sessionUsage: chatState.sessionUsage,
    currentContext: context,
    loading,
    runKind,
    compactError,
    sendError,
    sessions,
    activeSession,
    sessionLoading,
    pendingPermission,
    pending: chatState.pending,
    jobs: chatState.jobs,
    stopJob,
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
