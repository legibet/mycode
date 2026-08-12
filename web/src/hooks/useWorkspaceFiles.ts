/**
 * Fetch workspace file/dir candidates for the @ completion menu. Debounced,
 * with no cross-request caching so new and deleted files appear immediately.
 */

import { useEffect, useState } from "react";
import type { WorkspaceFilesResponse } from "../utils/completion";

interface WorkspaceFilesState {
  entries: WorkspaceFilesResponse["entries"];
  loading: boolean;
  truncated: boolean;
}

const EMPTY: WorkspaceFilesResponse["entries"] = [];

export function useWorkspaceFiles(
  cwd: string,
  dir: string,
  prefix: string,
  enabled: boolean,
): WorkspaceFilesState {
  const [state, setState] = useState<WorkspaceFilesState>({
    entries: EMPTY,
    loading: false,
    truncated: false,
  });

  useEffect(() => {
    if (!enabled) {
      setState({
        entries: EMPTY,
        loading: false,
        truncated: false,
      });
      return;
    }

    setState((prev) => ({ ...prev, loading: true }));
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ cwd, dir, prefix });
        const res = await fetch(`/api/workspaces/files?${params}`, {
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`status ${res.status}`);
        const data = (await res.json()) as WorkspaceFilesResponse;
        setState({
          entries: data.error ? EMPTY : data.entries,
          loading: false,
          truncated: data.truncated,
        });
      } catch (e) {
        if (controller.signal.aborted) return;
        console.error("Failed to list workspace files:", e);
        setState({
          entries: EMPTY,
          loading: false,
          truncated: false,
        });
      }
    }, 120);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [cwd, dir, prefix, enabled]);

  return state;
}
