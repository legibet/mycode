/**
 * Workspace folder picker.
 *
 * Mobile  : bottom sheet with slide-up entrance.
 * Desktop : compact centered dialog with scale+fade entrance.
 *
 * Animation strategy: CSS @keyframes with fill-mode:both.
 * The `from` keyframe state is applied before the first paint,
 * so no rAF tricks or dual-state mounts are needed.
 */

import { ChevronLeft, Clock, Folder, X } from 'lucide-react'
import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import type {
  WorkspaceBrowseResponse,
  WorkspaceEntry,
  WorkspaceRootsResponse,
  WorkspaceState,
} from '../types'
import { cn } from '../utils/cn'

// ─── helpers ────────────────────────────────────────────────────────────────

const normalizeSlashes = (v: string): string => v.replace(/\\/g, '/')
const isAbsolutePath = (v: string): boolean => /^([a-zA-Z]:[\\/]|\/)/.test(v)

const matchRoot = (roots: string[], value: string): string | undefined => {
  const normalized = normalizeSlashes(value)
  const sorted = [...roots].sort((a, b) => b.length - a.length)
  return (
    sorted.find((root) => {
      const normRoot = normalizeSlashes(root).replace(/\/+$/, '')
      return normalized === normRoot || normalized.startsWith(`${normRoot}/`)
    }) || roots[0]
  )
}

const toRelativePath = (root: string, absolutePath: string): string => {
  const normRoot = normalizeSlashes(root).replace(/\/+$/, '')
  const normPath = normalizeSlashes(absolutePath)
  if (normPath === normRoot) return ''
  const rel = normPath.startsWith(normRoot)
    ? normPath.slice(normRoot.length)
    : normPath
  return rel.replace(/^\/+/, '')
}

const rootLabel = (value: string): string => {
  if (!value || value === '/' || value === '\\') return '/'
  const normalized = value.replace(/[\\/]+$/, '')
  if (/\/Users\/[^/]+$/.test(normalized) || /\/home\/[^/]+$/.test(normalized))
    return '~'
  const parts = normalized.split(/[/\\]/)
  return parts[parts.length - 1] || value
}

// ─── component ──────────────────────────────────────────────────────────────

interface WorkspacePickerProps {
  open: boolean
  onClose: () => void
  currentCwd?: string
  cwdHistory?: string[]
  onSelect: (cwd: string) => void
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

export function WorkspacePicker({
  open,
  onClose,
  currentCwd,
  cwdHistory = [],
  onSelect,
}: WorkspacePickerProps) {
  const [state, setState] = useState<WorkspaceState>({
    roots: [],
    root: '',
    path: '',
    current: '',
    entries: [],
    loading: false,
    error: '',
  })
  const [pathInput, setPathInput] = useState('')
  const browseTokenRef = useRef(0)
  const inputRef = useRef<HTMLInputElement | null>(null)

  // Focus input on desktop only (avoid keyboard pop-up on mobile)
  useEffect(() => {
    if (open && window.matchMedia('(min-width: 640px)').matches) {
      inputRef.current?.focus()
    }
  }, [open])

  // ── data ────────────────────────────────────────────────────────────────

  const loadRoots = useCallback(async (): Promise<string[]> => {
    const res = await fetch('/api/workspaces/roots')
    if (!res.ok) throw new Error('Failed to load roots')
    const data = (await res.json()) as WorkspaceRootsResponse
    return data.roots || []
  }, [])

  const browsePath = useCallback(async (root: string, path = '') => {
    const token = ++browseTokenRef.current
    setState((prev) => ({ ...prev, loading: true, error: '' }))
    try {
      const params = new URLSearchParams({ root })
      if (path) params.set('path', path)
      const res = await fetch(`/api/workspaces/browse?${params.toString()}`)
      if (!res.ok) throw new Error('Failed to browse directory')
      const data = (await res.json()) as WorkspaceBrowseResponse
      if (data.error) throw new Error(data.error)
      if (browseTokenRef.current !== token) return
      setState((prev) => ({
        ...prev,
        root: data.root,
        path: data.path,
        current: data.current,
        entries: data.entries || [],
        loading: false,
        error: '',
      }))
      setPathInput(data.current || '')
    } catch (e) {
      if (browseTokenRef.current !== token) return
      setState((prev) => ({
        ...prev,
        loading: false,
        error: getErrorMessage(e),
      }))
    }
  }, [])

  useEffect(() => {
    if (!open) return
    let active = true
    const init = async () => {
      try {
        const roots = await loadRoots()
        if (!active) return
        if (!roots.length) {
          setState((prev) => ({
            ...prev,
            roots: [],
            loading: false,
            error: 'No workspace roots configured.',
          }))
          return
        }
        setState((prev) => ({ ...prev, roots }))
        setPathInput('')
        if (currentCwd) {
          const root = matchRoot(roots, currentCwd)
          if (root) {
            await browsePath(root, toRelativePath(root, currentCwd))
          }
        } else {
          const firstRoot = roots[0]
          if (firstRoot) {
            await browsePath(firstRoot, '')
          }
        }
      } catch (e) {
        if (active) {
          setState((prev) => ({
            ...prev,
            loading: false,
            error: getErrorMessage(e),
          }))
        }
      }
    }
    init()
    return () => {
      active = false
    }
  }, [open, browsePath, currentCwd, loadRoots])

  // ── path input ──────────────────────────────────────────────────────────

  const partialFilter = useMemo(() => {
    const trimmed = pathInput.trim()
    if (!trimmed || !state.current) return ''
    const base = `${state.current.replace(/\/$/, '')}/`
    if (trimmed.startsWith(base)) {
      const rest = trimmed.slice(base.length)
      return rest.includes('/') ? '' : rest
    }
    return ''
  }, [pathInput, state.current])

  const filteredEntries = partialFilter
    ? state.entries.filter((e: WorkspaceEntry) =>
        e.name.toLowerCase().startsWith(partialFilter.toLowerCase()),
      )
    : state.entries

  const goToPath = async (value: string) => {
    const trimmed = value.trim()
    if (!trimmed || !state.roots.length) return
    if (isAbsolutePath(trimmed)) {
      const root = matchRoot(state.roots, trimmed)
      if (!root) {
        setState((prev) => ({
          ...prev,
          error: 'Path is outside any configured workspace root.',
        }))
        return
      }
      await browsePath(root, toRelativePath(root, trimmed))
    } else {
      const base = state.path ? `${state.path}/${trimmed}` : trimmed
      await browsePath(state.root, base)
    }
  }

  const handleGoParent = () => {
    if (!state.root || !state.path) return
    const segments = state.path.split('/')
    void browsePath(state.root, segments.slice(0, -1).join('/'))
  }

  const handleSelect = () => {
    if (state.current) {
      onSelect(state.current)
      onClose()
    }
  }

  const handleInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') void goToPath(pathInput)
    else if (e.key === 'Tab') {
      e.preventDefault()
      const firstEntry = filteredEntries[0]
      if (firstEntry) void browsePath(state.root, firstEntry.path)
    } else if (e.key === 'Escape') {
      setPathInput(state.current || '')
      onClose()
    }
  }

  // ── derived ─────────────────────────────────────────────────────────────

  const pathSegments = state.path ? state.path.split('/') : []
  const recentPaths = cwdHistory.filter((p) => p !== state.current)
  const showRecent = recentPaths.length > 0 && !partialFilter

  // ── render ───────────────────────────────────────────────────────────────

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-end sm:items-center sm:justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="Select Workspace"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-[3px] animate-backdrop-in"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Sheet / Dialog
          animate-sheet-in     → mobile: translateY(100% → 0), fill-mode:both
          sm:animate-dialog-in → desktop: scale+opacity, overrides mobile anim
          fill-mode:both means the `from` state is painted immediately —
          no flash, no rAF tricks needed. */}
      <div
        className={cn(
          'relative flex flex-col overflow-hidden',
          'bg-background border border-border/40 shadow-2xl',
          'w-full rounded-t-2xl max-h-[82vh]',
          'animate-sheet-in',
          'sm:rounded-xl sm:w-[440px] sm:max-h-[520px]',
          'sm:animate-dialog-in',
        )}
      >
        {/* Drag handle — mobile only */}
        <div
          className="sm:hidden flex justify-center pt-2.5 pb-1 shrink-0"
          aria-hidden="true"
        >
          <div className="w-10 h-[3px] rounded-full bg-border/60" />
        </div>

        {/* Nav bar */}
        <div className="flex items-center h-12 px-1.5 border-b border-border/30 shrink-0 gap-0.5">
          <button
            type="button"
            onClick={handleGoParent}
            disabled={!state.path}
            className={cn(
              'flex items-center justify-center w-9 h-9 rounded-lg shrink-0 cursor-pointer',
              'text-muted-foreground transition-colors duration-150',
              'hover:bg-muted/70 hover:text-foreground',
              'active:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              'disabled:opacity-25 disabled:cursor-not-allowed',
            )}
            aria-label="Go up one level"
          >
            <ChevronLeft className="h-4 w-4" />
          </button>

          {/* Breadcrumb — horizontally scrollable */}
          <div className="flex-1 flex items-center overflow-x-auto scrollbar-none min-w-0 px-1 gap-0.5">
            <button
              type="button"
              onClick={() => state.root && void browsePath(state.root, '')}
              className={cn(
                'shrink-0 px-1.5 py-0.5 rounded text-sm font-mono cursor-pointer',
                'transition-colors duration-150 hover:bg-muted/60 hover:text-foreground',
                pathSegments.length === 0
                  ? 'text-foreground font-medium'
                  : 'text-muted-foreground',
              )}
            >
              {rootLabel(state.root) || '~'}
            </button>
            {pathSegments.map((segment, index) => {
              const crumbPath = pathSegments.slice(0, index + 1).join('/')
              return (
                <div key={crumbPath} className="flex items-center shrink-0">
                  <span
                    className="text-border/50 select-none mx-0.5 text-sm"
                    aria-hidden="true"
                  >
                    /
                  </span>
                  <button
                    type="button"
                    onClick={() => void browsePath(state.root, crumbPath)}
                    className={cn(
                      'px-1.5 py-0.5 rounded text-sm font-mono cursor-pointer whitespace-nowrap',
                      'transition-colors duration-150 hover:bg-muted/60 hover:text-foreground',
                      index === pathSegments.length - 1
                        ? 'text-foreground font-medium'
                        : 'text-muted-foreground',
                    )}
                  >
                    {segment}
                  </button>
                </div>
              )
            })}
          </div>

          {/* Close — desktop only */}
          <button
            type="button"
            onClick={onClose}
            className={cn(
              'hidden sm:flex items-center justify-center w-9 h-9 rounded-lg shrink-0 cursor-pointer',
              'text-muted-foreground/60 transition-colors duration-150',
              'hover:bg-muted/70 hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
            aria-label="Close"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        {/* Path input */}
        <div className="px-4 py-2.5 border-b border-border/20 bg-muted/[0.08] shrink-0">
          <input
            ref={inputRef}
            name="workspace-path"
            value={pathInput}
            onChange={(e) => setPathInput(e.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="Filter or type a path…"
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            className="w-full bg-transparent text-xs font-mono focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring text-muted-foreground placeholder:text-muted-foreground/30 caret-accent"
            aria-label="Filter directories or enter a path"
          />
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto overscroll-contain">
          {state.loading && (
            <div className="flex items-center justify-center h-36">
              <div className="flex items-end gap-1">
                {[0, 1, 2].map((i) => (
                  <div
                    key={i}
                    className="w-[3px] rounded-full bg-muted-foreground/30 animate-pulse"
                    style={{
                      height: `${12 + i * 4}px`,
                      animationDelay: `${i * 120}ms`,
                    }}
                  />
                ))}
              </div>
            </div>
          )}

          {!state.loading && state.error && (
            <div className="flex items-center justify-center h-36 px-6 text-center">
              <p className="text-xs text-destructive leading-relaxed">
                {state.error}
              </p>
            </div>
          )}

          {!state.loading && !state.error && (
            <div className="py-1">
              {/* Recent */}
              {showRecent && (
                <>
                  <div className="px-4 pt-3 pb-1.5">
                    <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/40 select-none">
                      Recent
                    </span>
                  </div>
                  {recentPaths.map((path) => (
                    <button
                      type="button"
                      key={path}
                      onClick={() => {
                        onSelect(path)
                        onClose()
                      }}
                      className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left cursor-pointer transition-colors duration-100 hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
                    >
                      <Clock className="h-3.5 w-3.5 shrink-0 text-muted-foreground/35 group-hover:text-accent/60 transition-colors duration-100" />
                      <span className="truncate text-xs font-mono text-muted-foreground group-hover:text-foreground/80 transition-colors duration-100">
                        {path}
                      </span>
                    </button>
                  ))}
                  {filteredEntries.length > 0 && (
                    <div className="mx-4 mt-1 mb-1 border-b border-border/20" />
                  )}
                </>
              )}

              {/* Entries */}
              {filteredEntries.length === 0 && !showRecent && (
                <div className="flex flex-col items-center justify-center gap-3 h-36 text-muted-foreground/30">
                  <Folder className="h-10 w-10" strokeWidth={1} />
                  <p className="text-xs">This folder is empty</p>
                </div>
              )}
              <div className="sm:grid sm:grid-cols-2">
                {filteredEntries.map((entry) => (
                  <button
                    type="button"
                    key={entry.path}
                    onClick={() => void browsePath(state.root, entry.path)}
                    className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left cursor-pointer transition-colors duration-100 hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
                  >
                    <Folder className="h-3.5 w-3.5 shrink-0 text-accent/40 group-hover:text-accent/70 transition-colors duration-100" />
                    <span className="truncate text-sm font-mono text-foreground/70 group-hover:text-foreground transition-colors duration-100">
                      {entry.name}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-3 h-12 px-4 border-t border-border/20 bg-muted/[0.06] shrink-0">
          <span
            className="flex-1 min-w-0 truncate text-xs font-mono text-muted-foreground/50"
            title={state.current}
          >
            {state.current || '—'}
          </span>
          <button
            type="button"
            onClick={handleSelect}
            disabled={!state.current}
            className={cn(
              'shrink-0 px-3.5 h-7 rounded-md text-xs font-semibold cursor-pointer',
              'transition-colors duration-150',
              'bg-accent/15 text-accent border border-accent/20',
              'hover:bg-accent/25 hover:border-accent/40 active:bg-accent/30',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              'disabled:opacity-25 disabled:cursor-not-allowed',
            )}
          >
            Open
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
