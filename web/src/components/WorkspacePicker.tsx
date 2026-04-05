/**
 * Workspace folder picker.
 *
 * Mobile  : bottom sheet with slide-up entrance.
 * Desktop : compact centered dialog with scale+fade entrance.
 *
 * Path input acts as a filter when typing partial names,
 * or navigates directly when an absolute path is entered.
 * Tab auto-completes and enters the first matching folder.
 */

import { Clock, Folder, FolderOpen, Search, X } from 'lucide-react'
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
  const [filter, setFilter] = useState('')
  const browseTokenRef = useRef(0)
  const inputRef = useRef<HTMLInputElement | null>(null)

  // Focus input on desktop only (avoid keyboard pop-up on mobile)
  useEffect(() => {
    if (open && window.matchMedia('(min-width: 640px)').matches) {
      // Small delay so dialog animation can start first
      const timer = setTimeout(() => inputRef.current?.focus(), 50)
      return () => clearTimeout(timer)
    }
  }, [open])

  // Reset filter when directory changes
  const prevCurrentRef = useRef(state.current)
  if (prevCurrentRef.current !== state.current) {
    prevCurrentRef.current = state.current
    if (filter) setFilter('')
  }

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
        setFilter('')
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

  // ── filtering & navigation ─────────────────────────────────────────────

  const filteredEntries = useMemo(() => {
    const trimmed = filter.trim().toLowerCase()
    if (!trimmed) return state.entries
    return state.entries.filter((e: WorkspaceEntry) =>
      e.name.toLowerCase().includes(trimmed),
    )
  }, [filter, state.entries])

  const navigateToPath = useCallback(
    async (value: string) => {
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
        // Treat as relative path from current directory
        const base = state.path ? `${state.path}/${trimmed}` : trimmed
        await browsePath(state.root, base)
      }
    },
    [state.roots, state.root, state.path, browsePath],
  )

  const handleGoParent = useCallback(() => {
    if (!state.root || !state.path) return
    const segments = state.path.split('/')
    void browsePath(state.root, segments.slice(0, -1).join('/'))
  }, [state.root, state.path, browsePath])

  const handleSelect = useCallback(() => {
    if (state.current) {
      onSelect(state.current)
      onClose()
    }
  }, [state.current, onSelect, onClose])

  const handleInputKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        // If filter matches exactly one entry, enter it; otherwise navigate to typed path
        const singleMatch =
          filteredEntries.length === 1 ? filteredEntries[0] : null
        if (singleMatch) {
          void browsePath(state.root, singleMatch.path)
        } else if (filter.trim()) {
          void navigateToPath(filter)
        }
      } else if (e.key === 'Tab') {
        e.preventDefault()
        const firstEntry = filteredEntries[0]
        if (firstEntry) void browsePath(state.root, firstEntry.path)
      } else if (e.key === 'Escape') {
        if (filter) {
          setFilter('')
        } else {
          onClose()
        }
      } else if (e.key === 'Backspace' && !filter) {
        // Backspace on empty filter goes up one level
        handleGoParent()
      }
    },
    [
      filter,
      filteredEntries,
      state.root,
      browsePath,
      navigateToPath,
      onClose,
      handleGoParent,
    ],
  )

  // ── derived ─────────────────────────────────────────────────────────────

  const pathSegments = state.path ? state.path.split('/') : []
  const recentPaths = cwdHistory.filter((p) => p !== state.current)
  const showRecent = recentPaths.length > 0 && !filter.trim()

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

        {/* Breadcrumb header */}
        <div className="flex items-center min-h-[44px] px-4 border-b border-border/30 shrink-0 gap-1">
          <div className="flex-1 flex items-center overflow-x-auto scrollbar-none min-w-0 gap-0.5">
            <button
              type="button"
              onClick={() => state.root && void browsePath(state.root, '')}
              className={cn(
                'shrink-0 px-1.5 py-0.5 rounded text-xs font-mono cursor-pointer',
                'transition-colors hover:bg-muted/60 hover:text-foreground',
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
                    className="text-border select-none mx-0.5 text-xs"
                    aria-hidden="true"
                  >
                    /
                  </span>
                  <button
                    type="button"
                    onClick={() => void browsePath(state.root, crumbPath)}
                    className={cn(
                      'px-1.5 py-0.5 rounded text-xs font-mono cursor-pointer whitespace-nowrap',
                      'transition-colors hover:bg-muted/60 hover:text-foreground',
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

          <button
            type="button"
            onClick={onClose}
            className={cn(
              'flex items-center justify-center w-8 h-8 rounded-lg shrink-0 cursor-pointer',
              'text-muted-foreground/50 transition-colors',
              'hover:bg-muted/70 hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
            aria-label="Close"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        {/* Filter input */}
        <div className="flex items-center gap-2.5 px-4 py-2.5 border-b border-border/20 shrink-0">
          <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground/30" />
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
            className="w-full bg-transparent text-sm font-mono focus-visible:outline-none text-foreground placeholder:text-muted-foreground/30 caret-accent"
            aria-label="Filter directories or enter a path"
          />
          {filter && (
            <button
              type="button"
              onClick={() => setFilter('')}
              className="shrink-0 text-muted-foreground/40 hover:text-muted-foreground transition-colors"
              aria-label="Clear filter"
            >
              <X className="h-3 w-3" />
            </button>
          )}
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
                      className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left cursor-pointer transition-colors hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
                    >
                      <Clock className="h-3.5 w-3.5 shrink-0 text-muted-foreground/35 group-hover:text-accent/60 transition-colors" />
                      <span className="truncate text-xs font-mono text-muted-foreground group-hover:text-foreground/80 transition-colors">
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
                  <p className="text-xs">
                    {filter.trim() ? 'No matches' : 'This folder is empty'}
                  </p>
                </div>
              )}
              {filteredEntries.map((entry) => (
                <button
                  type="button"
                  key={entry.path}
                  onClick={() => void browsePath(state.root, entry.path)}
                  className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left cursor-pointer transition-colors hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
                >
                  <Folder className="h-3.5 w-3.5 shrink-0 text-accent/40 group-hover:text-accent/70 transition-colors" />
                  <span className="truncate text-sm font-mono text-foreground/70 group-hover:text-foreground transition-colors">
                    {entry.name}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-3 h-12 px-4 border-t border-border/20 shrink-0">
          <FolderOpen className="h-3.5 w-3.5 shrink-0 text-muted-foreground/30" />
          <span
            className="flex-1 min-w-0 truncate text-xs font-mono text-muted-foreground/60"
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
              'transition-colors',
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
