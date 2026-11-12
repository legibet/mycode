/**
 * The snapshot-plus-stream core shared by the chat view and the agent transcript:
 * the chat reducer, a session snapshot applied with its pending events, and
 * the followed run's stream with recovery when it drops. Owners add their
 * own requests and decide what a stream end means for them.
 */

import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { RunInfo, SessionResponse } from "@/api";
import { readSse, SSE_DONE } from "@/api";
import {
  type ChatAction,
  chatReducer,
  INITIAL_CHAT_STATE,
  routeRunEvents,
} from "@/hooks/chatState";
import type { PendingInputs } from "@/types";
import { buildRenderMessages, readUsageTotals } from "@/utils/messages";

/** How a followed stream ended, for the owner that started it. */
export interface StreamEnd {
  /** The stream reached `[DONE]`. */
  sawDone: boolean;
  /** The run was stopped or failed, or the stream dropped for good. */
  stopped: boolean;
  /** No newer stream took over and the session is still the one shown. */
  isCurrent: () => boolean;
}

interface SessionStreamOptions {
  /** The session the state starts out showing. */
  initialSessionId?: string;
  /** Reload the session after its stream dropped; throws when it cannot. */
  reload: (sessionId: string) => Promise<unknown>;
  /** A compact run failed. */
  onCompactError?: (message: string) => void;
}

export function useSessionStream({
  initialSessionId,
  reload,
  onCompactError,
}: SessionStreamOptions) {
  const [chatState, setChatState] = useState(() => ({
    ...INITIAL_CHAT_STATE,
    messageSessionId: initialSessionId ?? null,
  }));
  // The reducer runs eagerly into this ref so async request and stream code
  // reads the current state, not the one from the last render.
  const chatStateRef = useRef(chatState);
  const dispatch = useCallback((action: ChatAction) => {
    chatStateRef.current = chatReducer(chatStateRef.current, action);
    setChatState(chatStateRef.current);
  }, []);
  const streamAbortRef = useRef<AbortController | null>(null);
  const streamTokenRef = useRef(0);
  /** The run this view follows, or knows to be active from a snapshot. */
  const activeRunRef = useRef<RunInfo | null>(null);
  const optionsRef = useRef({ reload, onCompactError });

  useEffect(() => {
    optionsRef.current = { reload, onCompactError };
  }, [reload, onCompactError]);

  /** Abandon the followed stream; the run itself goes on. */
  const stop = useCallback(() => {
    streamTokenRef.current += 1;
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeRunRef.current = null;
  }, []);

  const isStreaming = useCallback(() => streamAbortRef.current !== null, []);

  /**
   * Follow a run's stream from `after`. Resolves null when a newer stream or
   * a stop took over; otherwise with how the stream ended.
   * @param interrupted The snapshot already showed the run stopped.
   */
  const follow = useCallback(
    async (
      run: RunInfo,
      sessionId: string,
      after = 0,
      interrupted = false,
    ): Promise<StreamEnd | null> => {
      let stopped = interrupted;
      let sawDone = false;

      const token = ++streamTokenRef.current;
      streamAbortRef.current?.abort();
      const controller = new AbortController();
      streamAbortRef.current = controller;
      activeRunRef.current = run;
      const kind = run.kind;
      // Whether this stream still drives the state: no newer stream, same session.
      const isCurrent = () =>
        streamTokenRef.current === token && chatStateRef.current.messageSessionId === sessionId;

      const recoverSession = async () => {
        if (!isCurrent()) return true;
        try {
          await optionsRef.current.reload(sessionId);
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
          optionsRef.current.onCompactError?.(message);
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
            optionsRef.current.onCompactError?.(routed.compactError);
          }
          for (const applied of routed.apply) {
            dispatch({ type: "apply_event", event: applied });
          }
        }

        // A compact run's marker and cost come from the persisted history.
        if (kind === "compact" && sawDone) {
          await recoverSession();
        }

        // A reload that finds the run still active follows it with a newer
        // stream; one that finds it over leaves this one to end here.
        if (!sawDone && !(await recoverSession())) markDisconnected();
      } catch (e) {
        if (!(e instanceof Error) || e.name !== "AbortError") {
          if (!(await recoverSession())) markDisconnected();
        }
      }

      if (streamTokenRef.current !== token) return null;
      streamAbortRef.current = null;
      activeRunRef.current = null;
      return { sawDone, stopped, isCurrent };
    },
    [dispatch],
  );

  /**
   * Show a session snapshot: its history, then its pending events through
   * the live stream's route. Returns the active run to follow and where.
   */
  const applySnapshot = useCallback(
    (data: SessionResponse<unknown>, sessionId: string, pending?: PendingInputs) => {
      const run = data.active_run;
      const replay = routeRunEvents(run?.kind ?? "chat", data.pending_events);
      startTransition(() => {
        dispatch({
          type: "set_messages",
          messages: data.messages,
          sessionId,
          sessionUsage: readUsageTotals(data.session_usage, data.session_cost),
          replayEvents: replay.apply,
          expectedSessionId: sessionId,
          ...(pending ? { pending } : {}),
          jobs: data.jobs,
        });
      });
      activeRunRef.current = run;
      return {
        run,
        // The history covers every event up to the run's last seq when none is pending.
        after: data.pending_events.at(-1)?.seq ?? run?.last_seq ?? 0,
        stopped: replay.stopped,
        compactError: replay.compactError,
      };
    },
    [dispatch],
  );

  useEffect(() => stop, [stop]);

  const runningJobCalls = useMemo(
    () => new Set(chatState.jobs.map((job) => job.tool_use_id)),
    [chatState.jobs],
  );
  const messages = useMemo(
    () => buildRenderMessages(chatState.rawMessages, chatState.toolRuntimeById, runningJobCalls),
    [chatState.rawMessages, chatState.toolRuntimeById, runningJobCalls],
  );

  return {
    chatState,
    chatStateRef,
    dispatch,
    messages,
    activeRunRef,
    isStreaming,
    follow,
    stop,
    applySnapshot,
  };
}
