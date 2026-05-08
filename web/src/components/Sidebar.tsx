/**
 * Sidebar — brand, workspace, session history, settings.
 *
 * Top: mycode wordmark on its own line.
 * Below: workspace block (basename + full path, both clickable) with a
 *   `+` new-chat button to the right.
 * Sessions grouped by time bucket. Active session marked by a left accent bar.
 * Footer: single gear icon opening the settings panel (theme + global config).
 */

import { Plus, Settings as SettingsIcon, Terminal, Trash2 } from 'lucide-react'
import { type CSSProperties, memo, useMemo, useRef, useState } from 'react'
import type { LocalConfig, RemoteConfig, SessionSummary } from '../types'
import { cn } from '../utils/cn'
import {
  clampSidebarWidth,
  getMaxSidebarWidth,
  SIDEBAR_MIN_WIDTH,
} from '../utils/sidebar'
import { WorkspacePicker } from './WorkspacePicker'

// ─── path helpers ───────────────────────────────────────────────────────────

function basename(path: string): string {
  if (!path || path === '.') return '~'
  const cleaned = path.replace(/\/+$/, '')
  const segs = cleaned.split(/[/\\]/)
  return segs[segs.length - 1] || cleaned
}

function prettyPath(path: string): string {
  if (!path || path === '.') return ''
  const home = path.match(/^(\/Users\/[^/]+|\/home\/[^/]+)(.*)$/)
  if (home) return `~${home[2] || ''}`
  return path
}

// ─── time grouping ──────────────────────────────────────────────────────────

type Bucket = 'today' | 'yesterday' | 'week' | 'older'
const BUCKET_LABEL: Record<Bucket, string> = {
  today: 'today',
  yesterday: 'yesterday',
  week: 'this week',
  older: 'older',
}

function bucketOf(date: Date, now: Date): Bucket {
  const dayMs = 24 * 60 * 60 * 1000
  const startOfToday = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).getTime()
  const diff = startOfToday - date.getTime()
  if (diff < dayMs && date.getTime() >= startOfToday) return 'today'
  if (diff < 2 * dayMs) return 'yesterday'
  if (diff < 7 * dayMs) return 'week'
  return 'older'
}

function parseDate(value: string | undefined): Date | null {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

function formatOlder(date: Date): string {
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
  })
}

// ─── component ──────────────────────────────────────────────────────────────

interface SidebarProps {
  className?: string
  sessions: SessionSummary[]
  activeSession: SessionSummary | null
  onSelectSession: (id: string) => void
  onCreateSession: () => void
  onDeleteSession: (id: string) => Promise<void>
  config: LocalConfig
  remoteConfig: RemoteConfig | null
  cwdHistory: string[]
  onUpdateConfig: (config: LocalConfig) => void
  onRemoveHistory: (cwd: string) => void
  onOpenSettings: () => void
  workspaceMissing?: boolean
  width: number
  onResize?: (width: number) => void
  onResizeReset?: () => void
}

function SidebarResizer({
  width,
  onResize,
  onResizeReset,
}: {
  width: number
  onResize: (width: number) => void
  onResizeReset: () => void
}) {
  const [dragging, setDragging] = useState(false)

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    const startX = e.clientX
    const startWidth = width
    setDragging(true)
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const onMove = (ev: PointerEvent) => {
      onResize(clampSidebarWidth(startWidth + (ev.clientX - startX)))
    }
    const onUp = () => {
      setDragging(false)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 32 : 8
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      onResize(clampSidebarWidth(width - step))
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      onResize(clampSidebarWidth(width + step))
    } else if (e.key === 'Home') {
      e.preventDefault()
      onResizeReset()
    }
  }

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
        'group/resizer hidden md:block',
        'absolute top-0 bottom-0 -right-0.75 w-1.5 z-20 cursor-col-resize',
        'focus-visible:outline-none',
      )}
    >
      <div
        className={cn(
          'absolute inset-y-0 right-0.75 w-px transition-colors',
          dragging
            ? 'bg-accent'
            : 'bg-transparent group-hover/resizer:bg-accent/60 group-focus-visible/resizer:bg-accent/60',
        )}
      />
    </div>
  )
}

export const Sidebar = memo(function Sidebar({
  className,
  sessions,
  activeSession,
  onSelectSession,
  onCreateSession,
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
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const workspaceOpenedWithKeyboardRef = useRef(false)
  const resolvedCwd = config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd
  const wsName = basename(resolvedCwd)
  const wsPath = prettyPath(resolvedCwd)

  const groups = useMemo(() => {
    const now = new Date()
    const real = sessions.filter((s) => !s.isDraft)

    const buckets: Record<Bucket, SessionSummary[]> = {
      today: [],
      yesterday: [],
      week: [],
      older: [],
    }
    for (const s of real) {
      const d = parseDate(s.updated_at) || parseDate(s.created_at)
      const b = d ? bucketOf(d, now) : 'older'
      buckets[b].push(s)
    }
    const groups: { bucket: Bucket; sessions: SessionSummary[] }[] = []
    for (const bucket of ['today', 'yesterday', 'week', 'older'] as Bucket[]) {
      const bucketSessions = buckets[bucket]
      if (bucketSessions.length > 0) {
        groups.push({ bucket, sessions: bucketSessions })
      }
    }
    return groups
  }, [sessions])

  return (
    <div
      className={cn(
        'relative flex w-64 md:w-(--sidebar-width) flex-col border-r border-border/50 bg-sidebar-bg',
        className,
      )}
      style={{ '--sidebar-width': `${width}px` } as CSSProperties}
    >
      {/* Brand */}
      <div className="shrink-0 flex items-center gap-2.5 px-5 pt-5 pb-5">
        <Terminal className="h-4 w-4 text-foreground" aria-hidden="true" />
        <span className="font-display text-[15px] leading-none tracking-tight text-foreground font-medium">
          mycode
        </span>
      </div>

      {/* Workspace block + new-chat button */}
      <div className="shrink-0 px-5 mb-5">
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            onPointerDown={() => {
              workspaceOpenedWithKeyboardRef.current = false
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                workspaceOpenedWithKeyboardRef.current = true
              }
            }}
            onClick={() => setWorkspaceOpen(true)}
            title={resolvedCwd}
            aria-label={`Workspace: ${resolvedCwd}. Click to switch.`}
            className={cn(
              'group/ws flex flex-col items-start gap-0.5 min-w-0 flex-1 text-left',
              'focus-visible:outline-none',
            )}
          >
            <span className="font-mono text-[13px] leading-snug text-foreground/90 group-hover/ws:text-foreground transition-colors truncate max-w-full">
              {wsName}
            </span>
            {wsPath && (
              <span className="font-mono text-[11px] leading-snug text-muted-foreground/60 group-hover/ws:text-muted-foreground/85 transition-colors break-all">
                {wsPath}
              </span>
            )}
            {workspaceMissing && (
              <span className="font-mono text-[10px] leading-snug text-muted-foreground/55">
                missing
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={onCreateSession}
            aria-label="New chat"
            title="New chat"
            className={cn(
              'shrink-0 h-6 w-6 mt-0.5 flex items-center justify-center rounded',
              'text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors',
              'focus-visible:outline-none focus-visible:bg-muted/60',
            )}
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <WorkspacePicker
        open={workspaceOpen}
        openedWithKeyboard={workspaceOpenedWithKeyboardRef.current}
        onClose={() => setWorkspaceOpen(false)}
        currentCwd={config.cwd}
        cwdHistory={cwdHistory}
        onSelect={(cwd) => onUpdateConfig({ ...config, cwd })}
        onMissingHistory={onRemoveHistory}
      />

      {/* Session list */}
      <div className="flex-1 overflow-y-auto scrollbar-subtle">
        {groups.length === 0 ? (
          <div className="px-5 py-12 text-[12px] text-muted-foreground/50">
            no history yet
          </div>
        ) : (
          groups.map(({ bucket, sessions: items }, gi) => (
            <div key={bucket} className={cn(gi > 0 && 'mt-4')}>
              <div className="px-5 pb-1 text-[11px] tracking-wide text-muted-foreground/55 lowercase">
                {BUCKET_LABEL[bucket]}
              </div>
              {items.map((session) => {
                const isActive = activeSession?.id === session.id
                const isRunning = session.is_running
                const d =
                  parseDate(session.updated_at) || parseDate(session.created_at)
                const showOlderDate = bucket === 'older' && d
                return (
                  <div key={session.id} className="group relative">
                    <span
                      className={cn(
                        'absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r',
                        isActive ? 'bg-accent' : 'bg-transparent',
                      )}
                      aria-hidden="true"
                    />
                    <button
                      type="button"
                      aria-current={isActive ? 'true' : undefined}
                      className={cn(
                        'flex w-full items-center gap-2 pl-5 pr-9 py-1.5 text-[13px] text-left transition-colors',
                        isActive
                          ? 'text-foreground bg-accent/10'
                          : 'text-muted-foreground hover:text-foreground hover:bg-muted/40',
                      )}
                      onClick={() => onSelectSession(session.id)}
                    >
                      <span className="truncate flex-1">
                        {session.title || 'New Chat'}
                      </span>
                      {isRunning && (
                        <span
                          className="shrink-0 h-1.5 w-1.5 rounded-full bg-accent animate-breathing"
                          role="img"
                          aria-label="Running"
                        />
                      )}
                      {showOlderDate && !isRunning && (
                        <span className="shrink-0 text-[10px] font-mono text-muted-foreground/45">
                          {formatOlder(d)}
                        </span>
                      )}
                    </button>
                    <button
                      type="button"
                      aria-label="Delete session"
                      title="Delete"
                      onClick={(e) => {
                        e.stopPropagation()
                        void onDeleteSession(session.id)
                      }}
                      className={cn(
                        'absolute right-3 top-1/2 -translate-y-1/2 h-5 w-5 flex items-center justify-center rounded',
                        'opacity-0 group-hover:opacity-100 max-md:opacity-100',
                        'text-muted-foreground/60 hover:text-destructive hover:bg-destructive/10 transition-all',
                      )}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                )
              })}
            </div>
          ))
        )}
      </div>

      {/* Footer — single settings entry */}
      <div className="shrink-0 px-3 py-3 flex items-center">
        <button
          type="button"
          onClick={onOpenSettings}
          aria-label="Open settings"
          title="Settings"
          className={cn(
            'h-7 w-7 flex items-center justify-center rounded-md transition-colors',
            'text-muted-foreground hover:text-foreground hover:bg-muted/50',
            'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
          )}
        >
          <SettingsIcon className="h-3.5 w-3.5" />
        </button>
      </div>

      {onResize && onResizeReset && (
        <SidebarResizer
          width={width}
          onResize={onResize}
          onResizeReset={onResizeReset}
        />
      )}
    </div>
  )
})
