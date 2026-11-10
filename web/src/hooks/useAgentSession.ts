/**
 * A subagent's session for the transcript dialog: its snapshot and its running
 * turn's stream, through the same core as the chat view. The snapshot loads
 * on mount; again when a run of the child starts, since the transcript can
 * open before the child's first run or while it is idle; and when the app's
 * `/api/events` reconnects, since events have no replay.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { AgentSession, AgentSessionResponse, RunInfo } from "@/api";
import { readApiError } from "@/api";
import type { SubscribeServerEvents } from "@/hooks/useChat";
import { useSessionStream } from "@/hooks/useSessionStream";

export function useAgentSession(
  parentId: string,
  childId: string,
  subscribeEvents: SubscribeServerEvents,
) {
  // The run manager's key for the child's runs, and this view's session id.
  const key = `${parentId}/${childId}`;
  const url = `/api/sessions/${encodeURIComponent(parentId)}/agents/${encodeURIComponent(childId)}`;
  const [session, setSession] = useState<AgentSession | null>(null);
  /** A run of the child is being followed. */
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadTokenRef = useRef(0);
  /** Aborts the snapshot requests in flight when the view unmounts. */
  const loadAbortRef = useRef<AbortController | null>(null);
  const loadRef = useRef<() => Promise<void>>(async () => {});
  const reload = useCallback(() => loadRef.current(), []);
  const { chatState, messages, activeRunRef, follow, applySnapshot } = useSessionStream({
    initialSessionId: key,
    reload,
  });

  const refresh = useCallback(() => {
    void loadRef.current().catch((e: unknown) => {
      if (e instanceof Error && e.name === "AbortError") return;
      setError(e instanceof Error ? e.message : "Failed to load the agent");
    });
  }, []);

  const followRun = useCallback(
    async (run: RunInfo, after: number, stopped: boolean) => {
      setLive(true);
      const end = await follow(run, key, after, stopped);
      if (!end) return;
      setLive(false);
      // The turn's outcome is in agent.json once its stream is done.
      if (end.sawDone && end.isCurrent()) refresh();
    },
    [follow, key, refresh],
  );

  const load = useCallback(async () => {
    const token = ++loadTokenRef.current;
    const res = await fetch(url, { signal: loadAbortRef.current?.signal ?? null });
    if (token !== loadTokenRef.current) return;
    if (!res.ok) throw await readApiError(res, "Failed to load the agent");
    const data = (await res.json()) as AgentSessionResponse;
    if (token !== loadTokenRef.current) return;
    setError(null);
    setSession(data.session);
    const { run, after, stopped } = applySnapshot(data, key);
    if (run) void followRun(run, after, stopped);
  }, [applySnapshot, followRun, key, url]);

  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  // Load on mount. A response that lands after unmount must not open a stream.
  useEffect(() => {
    const controller = new AbortController();
    loadAbortRef.current = controller;
    refresh();
    return () => {
      loadTokenRef.current += 1;
      controller.abort();
    };
  }, [refresh]);

  useEffect(
    () =>
      subscribeEvents({
        onEvent: (event) => {
          if (event.type !== "run_started" || event.session_id !== key) return;
          if (event.run.id !== activeRunRef.current?.id) refresh();
        },
        onReconnect: refresh,
      }),
    [activeRunRef, key, refresh, subscribeEvents],
  );

  /** When the running turn started: its opening user message. A message
   * steered into the turn continues it rather than opening a new one. */
  const turnStartedAt = useMemo(() => {
    const opening = chatState.rawMessages.findLast(
      (message) =>
        message.role === "user" &&
        !message.meta?.steer &&
        message.content.some((block) => block.type !== "tool_result"),
    );
    return opening?.meta?.created_at;
  }, [chatState.rawMessages]);

  return { session, messages, live, error, turnStartedAt };
}
