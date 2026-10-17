/**
 * Fetch workspace file/dir candidates for the @ completion menu. Debounced,
 * with no cross-request caching so new and deleted files appear immediately.
 */

import { useEffect, useState } from "react";

import { fetchJson, type WorkspaceFilesResponse } from "@/api";

interface WorkspaceFilesState {
  entries: WorkspaceFilesResponse["entries"];
  loading: boolean;
  truncated: boolean;
}

const EMPTY_STATE: WorkspaceFilesState = {
  entries: [],
  loading: false,
  truncated: false,
};

export function useWorkspaceFiles(
  cwd: string,
  dir: string,
  prefix: string,
  enabled: boolean,
): WorkspaceFilesState {
  // Results are tagged with their query so `loading` derives from whether the
  // latest result answers the current query; stale entries stay visible meanwhile.
  const query = enabled ? JSON.stringify([cwd, dir, prefix]) : null;
  const [result, setResult] = useState<(WorkspaceFilesState & { query: string }) | null>(null);
  if (query === null && result !== null) setResult(null);

  useEffect(() => {
    if (query === null) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ cwd, dir, prefix });
        const data = await fetchJson<WorkspaceFilesResponse>(`/api/workspaces/files?${params}`, {
          signal: controller.signal,
        });
        setResult({
          query,
          entries: data.error ? [] : data.entries,
          loading: false,
          truncated: data.truncated,
        });
      } catch (e) {
        if (controller.signal.aborted) return;
        console.error("Failed to list workspace files:", e);
        setResult({ query, ...EMPTY_STATE });
      }
    }, 120);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, cwd, dir, prefix]);

  if (query === null) return EMPTY_STATE;
  if (result === null) return { ...EMPTY_STATE, loading: true };
  return { ...result, loading: result.query !== query };
}
