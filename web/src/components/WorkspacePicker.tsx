/**
 * Workspace folder picker.
 *
 * Path input acts as a filter when typing partial names, or navigates
 * directly when an absolute path is entered. Tab auto-completes and enters
 * the first matching folder.
 */

import { Clock, Folder, FolderOpen, Search, X } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useCallback, useMemo, useRef, useState } from "react";
import useSWR from "swr";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/useMediaQuery";

import type { WorkspaceBrowseResponse, WorkspaceEntry, WorkspaceRootsResponse } from "../types";
import { cn } from "../utils/cn";
import { shouldAutoFocusTextInputOnOpen } from "../utils/focus";

// ─── helpers ────────────────────────────────────────────────────────────────

const normalizeSlashes = (v: string): string => v.replace(/\\/g, "/");
const isAbsolutePath = (v: string): boolean => /^([a-zA-Z]:[\\/]|\/)/.test(v);

const matchRoot = (roots: string[], value: string, fallback = true): string | undefined => {
  const normalized = normalizeSlashes(value);
  const sorted = roots.toSorted((a, b) => b.length - a.length);
  return (
    sorted.find((root) => {
      const normRoot = normalizeSlashes(root).replace(/\/+$/, "");
      return normalized === normRoot || normalized.startsWith(`${normRoot}/`);
    }) || (fallback ? roots[0] : undefined)
  );
};

const toRelativePath = (root: string, absolutePath: string): string => {
  const normRoot = normalizeSlashes(root).replace(/\/+$/, "");
  const normPath = normalizeSlashes(absolutePath);
  if (normPath === normRoot) return "";
  const rel = normPath.startsWith(normRoot) ? normPath.slice(normRoot.length) : normPath;
  return rel.replace(/^\/+/, "");
};

const EMPTY_HISTORY: string[] = [];
const EMPTY_ENTRIES: WorkspaceEntry[] = [];

const rootLabel = (value: string): string => {
  if (!value || value === "/" || value === "\\") return "/";
  const normalized = value.replace(/[\\/]+$/, "");
  if (/\/Users\/[^/]+$/.test(normalized) || /\/home\/[^/]+$/.test(normalized)) return "~";
  const parts = normalized.split(/[/\\]/);
  return parts[parts.length - 1] || value;
};

// ─── data fetching ──────────────────────────────────────────────────────────

async function rootsFetcher(url: string): Promise<string[]> {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Failed to load roots");
  const data = (await res.json()) as WorkspaceRootsResponse;
  return data.roots ?? [];
}

async function browseFetcher(url: string): Promise<WorkspaceBrowseResponse> {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Failed to browse directory");
  const data = (await res.json()) as WorkspaceBrowseResponse;
  if (data.error) throw new Error(data.error);
  return data;
}

const SWR_OPTS = { revalidateOnFocus: false } as const;

function buildBrowseKey(target: { root: string; path: string } | null): string | null {
  if (!target?.root) return null;
  const params = new URLSearchParams({ root: target.root });
  if (target.path) params.set("path", target.path);
  return `/api/workspaces/browse?${params.toString()}`;
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

function getInitialTarget(
  open: boolean,
  roots: string[],
  currentCwd?: string,
): { root: string; path: string } | null {
  if (!open || roots.length === 0) return null;
  const root = currentCwd ? matchRoot(roots, currentCwd) : roots[0];
  if (!root) return null;
  return {
    root,
    path: currentCwd ? toRelativePath(root, currentCwd) : "",
  };
}

// ─── component ──────────────────────────────────────────────────────────────

interface WorkspacePickerProps {
  open: boolean;
  openedWithKeyboard: boolean;
  onClose: () => void;
  currentCwd?: string;
  cwdHistory?: string[];
  onSelect: (cwd: string) => void;
  onMissingHistory?: (cwd: string) => void;
}

export function WorkspacePicker({
  open,
  openedWithKeyboard,
  onClose,
  currentCwd,
  cwdHistory = EMPTY_HISTORY,
  onSelect,
  onMissingHistory,
}: WorkspacePickerProps) {
  const isDesktop = useMediaQuery("(min-width: 640px)");

  const {
    data: roots = [],
    error: rootsError,
    isLoading: rootsLoading,
  } = useSWR<string[]>("/api/workspaces/roots", rootsFetcher, SWR_OPTS);

  const [targetOverride, setTargetOverride] = useState<{
    root: string;
    path: string;
  } | null>(null);
  const [uiError, setUiError] = useState("");
  const [filter, setFilter] = useState("");

  const inputRef = useRef<HTMLInputElement | null>(null);

  const initialTarget = useMemo(
    () => getInitialTarget(open, roots, currentCwd),
    [open, roots, currentCwd],
  );
  const requestedTarget = targetOverride ?? initialTarget;
  const browseKey = buildBrowseKey(requestedTarget);
  const { data: browseData, error: browseError } = useSWR<WorkspaceBrowseResponse>(
    browseKey,
    browseFetcher,
    SWR_OPTS,
  );
  const activeTarget = browseData
    ? { root: browseData.root, path: browseData.path }
    : requestedTarget;

  const root = activeTarget?.root ?? "";
  const path = activeTarget?.path ?? "";
  const entries = browseData?.entries ?? EMPTY_ENTRIES;
  const current = browseData?.current ?? "";

  const loading = rootsLoading || (Boolean(browseKey) && !browseData && !browseError);

  const filteredEntries = useMemo(() => {
    const trimmed = filter.trim().toLowerCase();
    if (!trimmed) return entries;
    return entries.filter((e: WorkspaceEntry) => e.name.toLowerCase().includes(trimmed));
  }, [filter, entries]);

  const browseTo = useCallback((nextRoot: string, nextPath: string) => {
    setUiError("");
    setFilter("");
    setTargetOverride((prev) =>
      prev?.root === nextRoot && prev.path === nextPath ? prev : { root: nextRoot, path: nextPath },
    );
  }, []);

  const navigateToPath = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      if (!trimmed || roots.length === 0) return;
      if (isAbsolutePath(trimmed)) {
        const matched = matchRoot(roots, trimmed, false);
        if (!matched) {
          setUiError("Path is outside any configured workspace root.");
          return;
        }
        browseTo(matched, toRelativePath(matched, trimmed));
      } else if (root) {
        const base = path ? `${path}/${trimmed}` : trimmed;
        browseTo(root, base);
      }
    },
    [roots, root, path, browseTo],
  );

  const handleGoParent = useCallback(() => {
    if (!root || !path) return;
    const segments = path.split("/");
    browseTo(root, segments.slice(0, -1).join("/"));
  }, [root, path, browseTo]);

  const handleSelect = useCallback(() => {
    if (current) {
      onSelect(current);
      onClose();
    }
  }, [current, onSelect, onClose]);

  const checkWorkspace = useCallback(
    async (cwd: string): Promise<boolean> => {
      try {
        const matched = matchRoot(roots, cwd, false);
        if (!matched) return false;
        const key = buildBrowseKey({
          root: matched,
          path: toRelativePath(matched, cwd),
        });
        if (!key) return false;
        const res = await fetch(key);
        if (!res.ok) return false;
        const data = (await res.json()) as WorkspaceBrowseResponse;
        return Boolean(data.current && !data.error);
      } catch {
        return false;
      }
    },
    [roots],
  );

  const handleSelectRecent = useCallback(
    async (cwd: string) => {
      if (!(await checkWorkspace(cwd))) {
        setUiError("Workspace no longer exists.");
        onMissingHistory?.(cwd);
        return;
      }
      onSelect(cwd);
      onClose();
    },
    [checkWorkspace, onMissingHistory, onSelect, onClose],
  );

  const handleInputKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        const singleMatch = filteredEntries.length === 1 ? filteredEntries[0] : null;
        if (singleMatch && root) {
          browseTo(root, singleMatch.path);
        } else if (filter.trim()) {
          navigateToPath(filter);
        }
      } else if (e.key === "Tab") {
        e.preventDefault();
        const firstEntry = filteredEntries[0];
        if (firstEntry && root) browseTo(root, firstEntry.path);
      } else if (e.key === "Escape" && filter) {
        e.preventDefault();
        e.stopPropagation();
        setFilter("");
      } else if (e.key === "Backspace" && !filter) {
        handleGoParent();
      }
    },
    [filter, filteredEntries, root, browseTo, navigateToPath, handleGoParent],
  );

  const pathSegments = path ? path.split("/") : [];
  const currentPath = current || currentCwd || "";
  const recentPaths = cwdHistory.filter((p) => p !== currentPath);
  const showRecent = recentPaths.length > 0 && !filter.trim();
  const noticeMessage =
    uiError || (browseError ? "Workspace no longer exists. Choose another workspace." : "");
  const blockingErrorMessage =
    (rootsError ? getErrorMessage(rootsError) : "") ||
    (!rootsLoading && roots.length === 0 ? "No workspace roots configured." : "");
  const showRoots = Boolean(browseError) && roots.length > 0 && !filter.trim();

  const body: ReactNode = (
    <>
      {/* Drag handle — mobile only */}
      {!isDesktop && (
        <div className="flex shrink-0 justify-center pt-2.5 pb-1" aria-hidden="true">
          <div className="h-0.75 w-10 rounded-full bg-border/60" />
        </div>
      )}

      {/* Breadcrumb header */}
      <div className="flex min-h-11 shrink-0 items-center gap-1 border-b border-border/30 px-4">
        <div className="flex min-w-0 flex-1 scrollbar-none items-center gap-0.5 overflow-x-auto">
          <button
            type="button"
            onClick={() => root && browseTo(root, "")}
            className={cn(
              "shrink-0 rounded-sm px-1.5 py-0.5 font-mono text-xs",
              "transition-[color,background-color,scale] duration-150 hover:bg-muted/60 hover:text-foreground active:scale-95",
              pathSegments.length === 0 ? "font-medium text-foreground" : "text-muted-foreground",
            )}
          >
            {rootLabel(root) || "~"}
          </button>
          {pathSegments.map((segment, index) => {
            const crumbPath = pathSegments.slice(0, index + 1).join("/");
            return (
              <div key={crumbPath} className="flex shrink-0 items-center">
                <span className="mx-0.5 text-xs text-border select-none" aria-hidden="true">
                  /
                </span>
                <button
                  type="button"
                  onClick={() => browseTo(root, crumbPath)}
                  className={cn(
                    "rounded-sm px-1.5 py-0.5 font-mono text-xs whitespace-nowrap",
                    "transition-[color,background-color,scale] duration-150 hover:bg-muted/60 hover:text-foreground active:scale-95",
                    index === pathSegments.length - 1
                      ? "font-medium text-foreground"
                      : "text-muted-foreground",
                  )}
                >
                  {segment}
                </button>
              </div>
            );
          })}
        </div>

        <button
          type="button"
          onClick={onClose}
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-lg",
            "text-muted-foreground/50 transition-[color,background-color,scale] duration-150 active:scale-95",
            "hover:bg-muted/70 hover:text-foreground",
            "focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
          )}
          aria-label="Close"
        >
          <X className="size-3.5" />
        </button>
      </div>

      {/* Filter input */}
      <div className="flex shrink-0 items-center gap-2.5 border-b border-border/20 px-4 py-2.5">
        <Search className="size-3.5 shrink-0 text-muted-foreground/30" />
        <input
          ref={inputRef}
          name="workspace-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          onKeyDown={handleInputKeyDown}
          placeholder="Filter or type a path…"
          spellCheck={false}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="off"
          className="w-full bg-transparent font-mono text-base text-foreground caret-accent placeholder:text-muted-foreground/30 focus-visible:outline-none sm:text-sm"
          aria-label="Filter directories or enter a path"
        />
        {filter && (
          <button
            type="button"
            onClick={() => setFilter("")}
            className="shrink-0 text-muted-foreground/40 transition-[color,scale] duration-150 hover:text-muted-foreground active:scale-95"
            aria-label="Clear filter"
          >
            <X className="size-3" />
          </button>
        )}
      </div>

      {/* Content */}
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {loading && (
          <div className="flex h-36 items-center justify-center">
            <div className="flex items-end gap-1">
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  className="w-0.75 animate-pulse rounded-full bg-muted-foreground/30"
                  style={{
                    height: `${12 + i * 4}px`,
                    animationDelay: `${i * 120}ms`,
                  }}
                />
              ))}
            </div>
          </div>
        )}

        {!loading && blockingErrorMessage && (
          <div className="flex h-36 items-center justify-center px-6 text-center">
            <p className="text-xs leading-relaxed text-destructive">{blockingErrorMessage}</p>
          </div>
        )}

        {!loading && !blockingErrorMessage && (
          <div className="py-1">
            {noticeMessage && (
              <div className="mx-4 my-2 rounded-md border border-border/30 bg-muted/20 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                {noticeMessage}
              </div>
            )}

            {/* Recent */}
            {showRecent && (
              <>
                <div className="px-4 pt-3 pb-1.5">
                  <span className="text-[10px] font-semibold tracking-widest text-muted-foreground/40 uppercase select-none">
                    Recent
                  </span>
                </div>
                {recentPaths.map((p) => (
                  <button
                    type="button"
                    key={p}
                    onClick={() => void handleSelectRecent(p)}
                    className="group flex min-h-11 w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none active:bg-muted/70"
                  >
                    <Clock className="size-3.5 shrink-0 text-muted-foreground/35 transition-colors group-hover:text-accent/60" />
                    <span className="truncate font-mono text-xs text-muted-foreground transition-colors group-hover:text-foreground/80">
                      {p}
                    </span>
                  </button>
                ))}
                {(filteredEntries.length > 0 || showRoots) && (
                  <div className="mx-4 mt-1 mb-1 border-b border-border/20" />
                )}
              </>
            )}

            {showRoots && (
              <>
                <div className="px-4 pt-3 pb-1.5">
                  <span className="text-[10px] font-semibold tracking-widest text-muted-foreground/40 uppercase select-none">
                    Roots
                  </span>
                </div>
                {roots.map((workspaceRoot) => (
                  <button
                    type="button"
                    key={workspaceRoot}
                    onClick={() => browseTo(workspaceRoot, "")}
                    className="group flex min-h-11 w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none active:bg-muted/70"
                  >
                    <FolderOpen className="size-3.5 shrink-0 text-muted-foreground/35 transition-colors group-hover:text-accent/60" />
                    <span className="truncate font-mono text-xs text-muted-foreground transition-colors group-hover:text-foreground/80">
                      {workspaceRoot}
                    </span>
                  </button>
                ))}
                {filteredEntries.length > 0 && (
                  <div className="mx-4 mt-1 mb-1 border-b border-border/20" />
                )}
              </>
            )}

            {/* Entries */}
            {filteredEntries.length === 0 && !showRecent && !showRoots && (
              <div className="flex h-36 flex-col items-center justify-center gap-3 text-muted-foreground/30">
                <Folder className="size-10" strokeWidth={1} />
                <p className="text-xs">{filter.trim() ? "No matches" : "This folder is empty"}</p>
              </div>
            )}
            {filteredEntries.map((entry) => (
              <button
                type="button"
                key={entry.path}
                onClick={() => browseTo(root, entry.path)}
                className="group flex min-h-11 w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none active:bg-muted/70"
              >
                <Folder className="size-3.5 shrink-0 text-accent/40 transition-colors group-hover:text-accent/70" />
                <span className="truncate font-mono text-sm text-foreground/70 transition-colors group-hover:text-foreground">
                  {entry.name}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex h-12 shrink-0 items-center gap-3 border-t border-border/20 px-4">
        <FolderOpen className="size-3.5 shrink-0 text-muted-foreground/30" />
        <span
          className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground/60"
          title={current}
        >
          {current || "—"}
        </span>
        <button
          type="button"
          onClick={handleSelect}
          disabled={!current}
          className={cn(
            "h-7 shrink-0 rounded-md px-3.5 text-xs font-semibold",
            "transition-[color,background-color,border-color,scale] duration-150 active:scale-95",
            "border border-accent/20 bg-accent/15 text-accent",
            "hover:border-accent/40 hover:bg-accent/25 active:bg-accent/30",
            "focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            "disabled:cursor-not-allowed disabled:opacity-25",
          )}
        >
          Open
        </button>
      </div>
    </>
  );

  const handleOpenChange = (next: boolean) => {
    if (!next) onClose();
  };

  const initialFocus = shouldAutoFocusTextInputOnOpen(openedWithKeyboard) ? inputRef : undefined;

  if (isDesktop) {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent
          showCloseButton={false}
          initialFocus={initialFocus}
          className="flex max-h-130 w-110 max-w-[calc(100vw-2rem)] flex-col gap-0 p-0 sm:max-w-[calc(100vw-2rem)]"
        >
          <DialogTitle className="sr-only">Select workspace</DialogTitle>
          {body}
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        initialFocus={initialFocus}
        className="flex max-h-[82vh] flex-col gap-0 rounded-t-2xl p-0"
      >
        <SheetTitle className="sr-only">Select workspace</SheetTitle>
        {body}
      </SheetContent>
    </Sheet>
  );
}
