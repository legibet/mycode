/**
 * Sidebar — brand, workspace, session history, settings.
 *
 * Top: mycode wordmark on its own line.
 * Below: workspace block (basename + full path, both clickable) with
 *   search and `+` new-chat buttons to the right.
 * Sessions grouped by time bucket. Active session marked by a left accent bar.
 * Footer: single gear icon opening the settings panel (theme + global config).
 */

import { Plus, Search, Settings as SettingsIcon, Terminal, Trash2 } from "lucide-react";
import { type CSSProperties, memo, useMemo, useRef, useState } from "react";

import type { LocalConfig, RemoteConfig, SessionSummary } from "../types";
import { cn } from "../utils/cn";
import { formatShortDate, parseDate, prettifyPath } from "../utils/format";
import { isMac } from "../utils/platform";
import { clampSidebarWidth, getMaxSidebarWidth, SIDEBAR_MIN_WIDTH } from "../utils/sidebar";
import { WorkspacePicker } from "./WorkspacePicker";

// ─── path helpers ───────────────────────────────────────────────────────────

function basename(path: string): string {
  if (!path || path === ".") return "~";
  const cleaned = path.replace(/\/+$/, "");
  const segs = cleaned.split(/[/\\]/);
  return segs[segs.length - 1] || cleaned;
}

function prettyPath(path: string): string {
  if (!path || path === ".") return "";
  return prettifyPath(path);
}

// ─── time grouping ──────────────────────────────────────────────────────────

type Bucket = "today" | "yesterday" | "week" | "older";
const BUCKET_LABEL: Record<Bucket, string> = {
  today: "today",
  yesterday: "yesterday",
  week: "this week",
  older: "older",
};

function bucketOf(date: Date, now: Date): Bucket {
  const dayMs = 24 * 60 * 60 * 1000;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const diff = startOfToday - date.getTime();
  if (diff < dayMs && date.getTime() >= startOfToday) return "today";
  if (diff < 2 * dayMs) return "yesterday";
  if (diff < 7 * dayMs) return "week";
  return "older";
}

// ─── component ──────────────────────────────────────────────────────────────

const headerButtonClass = cn(
  "flex size-6 items-center justify-center rounded-sm",
  "text-muted-foreground transition-[color,background-color,scale] duration-150 hover:bg-muted/60 hover:text-foreground active:scale-95",
  "focus-visible:bg-muted/60 focus-visible:outline-none",
);

interface SidebarProps {
  className?: string;
  sessions: SessionSummary[];
  activeSession: SessionSummary | null;
  onSelectSession: (id: string) => void;
  onCreateSession: () => void;
  onOpenSearch: () => void;
  onDeleteSession: (id: string) => Promise<void>;
  config: LocalConfig;
  remoteConfig: RemoteConfig | null;
  cwdHistory: string[];
  onUpdateConfig: (config: LocalConfig) => void;
  onRemoveHistory: (cwd: string) => void;
  onOpenSettings: () => void;
  workspaceMissing?: boolean;
  width: number;
  onResize?: (width: number) => void;
  onResizeReset?: () => void;
}

function SidebarResizer({
  width,
  onResize,
  onResizeReset,
}: {
  width: number;
  onResize: (width: number) => void;
  onResizeReset: () => void;
}) {
  const [dragging, setDragging] = useState(false);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = width;
    setDragging(true);
    Object.assign(document.body.style, {
      cursor: "col-resize",
      userSelect: "none",
    });

    const onMove = (ev: PointerEvent) => {
      onResize(clampSidebarWidth(startWidth + (ev.clientX - startX)));
    };
    const onUp = () => {
      setDragging(false);
      Object.assign(document.body.style, {
        cursor: "",
        userSelect: "",
      });
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 32 : 8;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      onResize(clampSidebarWidth(width - step));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      onResize(clampSidebarWidth(width + step));
    } else if (e.key === "Home") {
      e.preventDefault();
      onResizeReset();
    }
  };

  return (
    // biome-ignore lint/a11y/useSemanticElements: <hr> is not interactive; ARIA window-splitter pattern uses role="separator" on a focusable div
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuemin={SIDEBAR_MIN_WIDTH}
      aria-valuemax={getMaxSidebarWidth()}
      aria-valuenow={width}
      tabIndex={0}
      onPointerDown={handlePointerDown}
      onDoubleClick={onResizeReset}
      onKeyDown={handleKeyDown}
      className={cn(
        "group/resizer hidden md:block",
        "absolute top-0 -right-0.75 bottom-0 z-20 w-1.5 cursor-col-resize",
        "focus-visible:outline-none",
      )}
    >
      <div
        className={cn(
          "absolute inset-y-0 right-0.75 w-px transition-colors",
          dragging
            ? "bg-accent"
            : "bg-transparent group-hover/resizer:bg-accent/60 group-focus-visible/resizer:bg-accent/60",
        )}
      />
    </div>
  );
}

export const Sidebar = memo(function Sidebar({
  className,
  sessions,
  activeSession,
  onSelectSession,
  onCreateSession,
  onOpenSearch,
  onDeleteSession,
  config,
  remoteConfig,
  cwdHistory,
  onUpdateConfig,
  onRemoveHistory,
  onOpenSettings,
  workspaceMissing = false,
  width,
  onResize,
  onResizeReset,
}: SidebarProps) {
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [workspaceOpenKey, setWorkspaceOpenKey] = useState(0);
  const workspaceOpenedWithKeyboardRef = useRef(false);
  const resolvedCwd = config.cwd === "." ? remoteConfig?.cwd || "." : config.cwd;
  const wsName = basename(resolvedCwd);
  const wsPath = prettyPath(resolvedCwd);

  const groups = useMemo(() => {
    const now = new Date();
    const buckets: Record<Bucket, { session: SessionSummary; date: Date | null }[]> = {
      today: [],
      yesterday: [],
      week: [],
      older: [],
    };
    for (const session of sessions) {
      if (session.isDraft) continue;
      const date = parseDate(session.updated_at) || parseDate(session.created_at);
      buckets[date ? bucketOf(date, now) : "older"].push({ session, date });
    }
    return (["today", "yesterday", "week", "older"] as Bucket[])
      .filter((bucket) => buckets[bucket].length > 0)
      .map((bucket) => ({ bucket, items: buckets[bucket] }));
  }, [sessions]);

  return (
    <div
      className={cn(
        "relative flex w-64 flex-col border-r border-border/50 bg-sidebar-bg md:w-(--sidebar-width)",
        className,
      )}
      style={{ "--sidebar-width": `${width}px` } as CSSProperties}
    >
      {/* Brand */}
      <div className="flex shrink-0 items-center gap-2.5 px-5 pt-5 pb-5">
        <Terminal className="size-4 text-foreground" aria-hidden="true" />
        <span className="font-display text-[15px] leading-none font-medium tracking-tight text-foreground">
          mycode
        </span>
      </div>

      {/* Workspace block + new-chat button */}
      <div className="mb-5 shrink-0 px-5">
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            onPointerDown={() => {
              workspaceOpenedWithKeyboardRef.current = false;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                workspaceOpenedWithKeyboardRef.current = true;
              }
            }}
            onClick={() => {
              setWorkspaceOpenKey((key) => key + 1);
              setWorkspaceOpen(true);
            }}
            title={resolvedCwd}
            aria-label={`Workspace: ${resolvedCwd}. Click to switch.`}
            className={cn(
              "group/ws flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left",
              "focus-visible:outline-none",
            )}
          >
            <span className="max-w-full truncate font-mono text-[13px] leading-snug text-foreground/90 transition-colors group-hover/ws:text-foreground">
              {wsName}
            </span>
            {wsPath && (
              <span className="font-mono text-[11px] leading-snug break-all text-muted-foreground/60 transition-colors group-hover/ws:text-muted-foreground/85">
                {wsPath}
              </span>
            )}
            {workspaceMissing && (
              <span className="font-mono text-[10px] leading-snug text-muted-foreground/55">
                missing
              </span>
            )}
          </button>
          <div className="mt-0.5 flex shrink-0 items-center gap-0.5">
            <button
              type="button"
              onClick={onOpenSearch}
              aria-label="Search chats"
              title={`Search chats (${isMac ? "⌘K" : "Ctrl+K"})`}
              className={headerButtonClass}
            >
              <Search className="size-3.5" />
            </button>
            <button
              type="button"
              onClick={onCreateSession}
              aria-label="New chat"
              title="New chat"
              className={headerButtonClass}
            >
              <Plus className="size-3.5" />
            </button>
          </div>
        </div>
      </div>

      <WorkspacePicker
        key={workspaceOpenKey}
        open={workspaceOpen}
        openedWithKeyboard={workspaceOpenedWithKeyboardRef.current}
        onClose={() => setWorkspaceOpen(false)}
        currentCwd={config.cwd}
        cwdHistory={cwdHistory}
        onSelect={(cwd) => onUpdateConfig({ ...config, cwd })}
        onMissingHistory={onRemoveHistory}
      />

      {/* Session list */}
      <div className="scrollbar-subtle flex-1 overflow-y-auto">
        {groups.length === 0 ? (
          <div className="px-5 py-12 text-[12px] text-muted-foreground/50">no history yet</div>
        ) : (
          groups.map(({ bucket, items }, gi) => (
            <div key={bucket} className={cn(gi > 0 && "mt-4")}>
              <div className="px-5 pb-1 text-[11px] tracking-wide text-muted-foreground/55 lowercase">
                {BUCKET_LABEL[bucket]}
              </div>
              {items.map(({ session, date }) => {
                const isActive = activeSession?.id === session.id;
                const isRunning = session.is_running;
                const showOlderDate = bucket === "older" && date;
                return (
                  <div key={session.id} className="group relative">
                    <span
                      className={cn(
                        "pointer-events-none absolute inset-y-0 right-2 left-0",
                        isActive ? "bg-accent/10" : "bg-transparent",
                      )}
                      aria-hidden="true"
                    />
                    <span
                      className={cn(
                        "absolute top-1.5 bottom-1.5 left-0 z-10 w-0.5 rounded-r",
                        isActive ? "bg-accent" : "bg-transparent",
                      )}
                      aria-hidden="true"
                    />
                    <button
                      type="button"
                      aria-current={isActive ? "true" : undefined}
                      className={cn(
                        "relative z-10 flex w-full items-center gap-2 py-1.5 pr-9 pl-5 text-left text-[13px] transition-colors",
                        isActive
                          ? "text-foreground"
                          : "text-muted-foreground hover:text-foreground",
                      )}
                      onClick={() => onSelectSession(session.id)}
                    >
                      <span className="flex-1 truncate">{session.title || "New Chat"}</span>
                      {isRunning && (
                        <span
                          className="size-1.5 shrink-0 animate-breathing rounded-full bg-accent"
                          role="img"
                          aria-label="Running"
                        />
                      )}
                      {showOlderDate && !isRunning && (
                        <span className="shrink-0 font-mono text-[10px] text-muted-foreground/45">
                          {formatShortDate(date)}
                        </span>
                      )}
                    </button>
                    <button
                      type="button"
                      aria-label="Delete session"
                      title="Delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        void onDeleteSession(session.id);
                      }}
                      className={cn(
                        "absolute top-1/2 right-3 z-20 flex size-5 -translate-y-1/2 items-center justify-center rounded-sm",
                        "opacity-0 group-hover:opacity-100 max-md:opacity-100",
                        "text-muted-foreground/60 transition-[color,background-color,scale] duration-150 hover:bg-destructive/10 hover:text-destructive active:scale-95",
                      )}
                    >
                      <Trash2 className="size-3" />
                    </button>
                  </div>
                );
              })}
            </div>
          ))
        )}
      </div>

      {/* Footer — single settings entry */}
      <div className="flex shrink-0 items-center p-3">
        <button
          type="button"
          onClick={onOpenSettings}
          aria-label="Open settings"
          title="Settings"
          className={cn(
            "flex size-7 items-center justify-center rounded-md transition-[color,background-color,scale] duration-150 active:scale-95",
            "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
            "focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none",
          )}
        >
          <SettingsIcon className="size-3.5" />
        </button>
      </div>

      {onResize && onResizeReset && (
        <SidebarResizer width={width} onResize={onResize} onResizeReset={onResizeReset} />
      )}
    </div>
  );
});
