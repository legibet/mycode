/**
 * Workspace folder picker.
 *
 * Path input acts as a filter when typing partial names, or navigates
 * directly when an absolute path is entered. Tab auto-completes and enters
 * the first matching folder.
 */

import { Clock, Folder, FolderOpen, Search, X } from 'lucide-react'
import {
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import useSWR from 'swr'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import type {
  WorkspaceBrowseResponse,
  WorkspaceEntry,
  WorkspaceRootsResponse,
} from '../types'
import { cn } from '../utils/cn'
import { shouldAutoFocusTextInputOnOpen } from '../utils/focus'

// ─── helpers ────────────────────────────────────────────────────────────────

const normalizeSlashes = (v: string): string => v.replace(/\\/g, '/')
const isAbsolutePath = (v: string): boolean => /^([a-zA-Z]:[\\/]|\/)/.test(v)

const matchRoot = (
  roots: string[],
  value: string,
  fallback = true,
): string | undefined => {
  const normalized = normalizeSlashes(value)
  const sorted = [...roots].sort((a, b) => b.length - a.length)
  return (
    sorted.find((root) => {
      const normRoot = normalizeSlashes(root).replace(/\/+$/, '')
      return normalized === normRoot || normalized.startsWith(`${normRoot}/`)
    }) || (fallback ? roots[0] : undefined)
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

const EMPTY_HISTORY: string[] = []

const rootLabel = (value: string): string => {
  if (!value || value === '/' || value === '\\') return '/'
  const normalized = value.replace(/[\\/]+$/, '')
  if (/\/Users\/[^/]+$/.test(normalized) || /\/home\/[^/]+$/.test(normalized))
    return '~'
  const parts = normalized.split(/[/\\]/)
  return parts[parts.length - 1] || value
}

// ─── data fetching ──────────────────────────────────────────────────────────

async function rootsFetcher(url: string): Promise<string[]> {
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to load roots')
  const data = (await res.json()) as WorkspaceRootsResponse
  return data.roots ?? []
}

async function browseFetcher(url: string): Promise<WorkspaceBrowseResponse> {
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to browse directory')
  const data = (await res.json()) as WorkspaceBrowseResponse
  if (data.error) throw new Error(data.error)
  return data
}

const ROOTS_OPTS = { revalidateOnFocus: false } as const
const BROWSE_OPTS = { revalidateOnFocus: false } as const

function buildBrowseKey(
  target: { root: string; path: string } | null,
): string | null {
  if (!target?.root) return null
  const params = new URLSearchParams({ root: target.root })
  if (target.path) params.set('path', target.path)
  return `/api/workspaces/browse?${params.toString()}`
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

// ─── component ──────────────────────────────────────────────────────────────

interface WorkspacePickerProps {
  open: boolean
  openedWithKeyboard: boolean
  onClose: () => void
  currentCwd?: string
  cwdHistory?: string[]
  onSelect: (cwd: string) => void
  onMissingHistory?: (cwd: string) => void
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
  const isDesktop = useMediaQuery('(min-width: 640px)')

  const {
    data: roots = [],
    error: rootsError,
    isLoading: rootsLoading,
  } = useSWR<string[]>('/api/workspaces/roots', rootsFetcher, ROOTS_OPTS)

  const [target, setTarget] = useState<{ root: string; path: string } | null>(
    null,
  )
  const [uiError, setUiError] = useState('')
  const [filter, setFilter] = useState('')

  const inputRef = useRef<HTMLInputElement | null>(null)

  const browseKey = buildBrowseKey(target)
  const { data: browseData, error: browseError } =
    useSWR<WorkspaceBrowseResponse>(browseKey, browseFetcher, BROWSE_OPTS)

  // Sync target with currentCwd whenever the picker opens or the host's cwd
  // changes. setState short-circuits when the target is already correct so a
  // re-open on the same cwd doesn't invalidate SWR caches.
  useEffect(() => {
    if (!open || roots.length === 0) return
    const desiredRoot = currentCwd ? matchRoot(roots, currentCwd) : roots[0]
    if (!desiredRoot) return
    const desiredPath = currentCwd
      ? toRelativePath(desiredRoot, currentCwd)
      : ''
    setTarget((prev) =>
      prev?.root === desiredRoot && prev.path === desiredPath
        ? prev
        : { root: desiredRoot, path: desiredPath },
    )
  }, [open, roots, currentCwd])

  useEffect(() => {
    if (!open) return
    setFilter('')
    setUiError('')
  }, [open])

  // The backend may normalize the requested path (strip trailing slashes,
  // collapse `..`, etc). Realign target so subsequent navigation uses the
  // canonical form.
  useEffect(() => {
    if (!browseData) return
    setTarget((prev) =>
      prev?.root === browseData.root && prev.path === browseData.path
        ? prev
        : { root: browseData.root, path: browseData.path },
    )
  }, [browseData])

  const root = target?.root ?? ''
  const path = target?.path ?? ''
  const entries = browseData?.entries ?? []
  const current = browseData?.current ?? ''

  const loading =
    rootsLoading || (Boolean(browseKey) && !browseData && !browseError)

  const prevCurrentRef = useRef(current)
  if (prevCurrentRef.current !== current) {
    prevCurrentRef.current = current
    if (filter) setFilter('')
  }

  const filteredEntries = useMemo(() => {
    const trimmed = filter.trim().toLowerCase()
    if (!trimmed) return entries
    return entries.filter((e: WorkspaceEntry) =>
      e.name.toLowerCase().includes(trimmed),
    )
  }, [filter, entries])

  const browseTo = useCallback((nextRoot: string, nextPath: string) => {
    setUiError('')
    setTarget((prev) =>
      prev?.root === nextRoot && prev.path === nextPath
        ? prev
        : { root: nextRoot, path: nextPath },
    )
  }, [])

  const navigateToPath = useCallback(
    (value: string) => {
      const trimmed = value.trim()
      if (!trimmed || roots.length === 0) return
      if (isAbsolutePath(trimmed)) {
        const matched = matchRoot(roots, trimmed, false)
        if (!matched) {
          setUiError('Path is outside any configured workspace root.')
          return
        }
        browseTo(matched, toRelativePath(matched, trimmed))
      } else if (target) {
        const base = target.path ? `${target.path}/${trimmed}` : trimmed
        browseTo(target.root, base)
      }
    },
    [roots, target, browseTo],
  )

  const handleGoParent = useCallback(() => {
    if (!target?.root || !target.path) return
    const segments = target.path.split('/')
    browseTo(target.root, segments.slice(0, -1).join('/'))
  }, [target, browseTo])

  const handleSelect = useCallback(() => {
    if (current) {
      onSelect(current)
      onClose()
    }
  }, [current, onSelect, onClose])

  const checkWorkspace = useCallback(
    async (cwd: string): Promise<boolean> => {
      try {
        const matched = matchRoot(roots, cwd, false)
        if (!matched) return false
        const key = buildBrowseKey({
          root: matched,
          path: toRelativePath(matched, cwd),
        })
        if (!key) return false
        const res = await fetch(key)
        if (!res.ok) return false
        const data = (await res.json()) as WorkspaceBrowseResponse
        return Boolean(data.current && !data.error)
      } catch {
        return false
      }
    },
    [roots],
  )

  const handleSelectRecent = useCallback(
    async (cwd: string) => {
      if (!(await checkWorkspace(cwd))) {
        setUiError('Workspace no longer exists.')
        onMissingHistory?.(cwd)
        return
      }
      onSelect(cwd)
      onClose()
    },
    [checkWorkspace, onMissingHistory, onSelect, onClose],
  )

  const handleInputKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        const singleMatch =
          filteredEntries.length === 1 ? filteredEntries[0] : null
        if (singleMatch && root) {
          browseTo(root, singleMatch.path)
        } else if (filter.trim()) {
          navigateToPath(filter)
        }
      } else if (e.key === 'Tab') {
        e.preventDefault()
        const firstEntry = filteredEntries[0]
        if (firstEntry && root) browseTo(root, firstEntry.path)
      } else if (e.key === 'Escape' && filter) {
        e.preventDefault()
        e.stopPropagation()
        setFilter('')
      } else if (e.key === 'Backspace' && !filter) {
        handleGoParent()
      }
    },
    [filter, filteredEntries, root, browseTo, navigateToPath, handleGoParent],
  )

  const pathSegments = path ? path.split('/') : []
  const currentPath = current || currentCwd || ''
  const recentPaths = cwdHistory.filter((p) => p !== currentPath)
  const showRecent = recentPaths.length > 0 && !filter.trim()
  const noticeMessage =
    uiError ||
    (browseError ? 'Workspace no longer exists. Choose another workspace.' : '')
  const blockingErrorMessage =
    (rootsError ? getErrorMessage(rootsError) : '') ||
    (!rootsLoading && roots.length === 0
      ? 'No workspace roots configured.'
      : '')
  const showRoots = Boolean(browseError) && roots.length > 0 && !filter.trim()

  const body: ReactNode = (
    <>
      {/* Drag handle — mobile only */}
      {!isDesktop && (
        <div
          className="flex justify-center pt-2.5 pb-1 shrink-0"
          aria-hidden="true"
        >
          <div className="w-10 h-[3px] rounded-full bg-border/60" />
        </div>
      )}

      {/* Breadcrumb header */}
      <div className="flex items-center min-h-[44px] px-4 border-b border-border/30 shrink-0 gap-1">
        <div className="flex-1 flex items-center overflow-x-auto scrollbar-none min-w-0 gap-0.5">
          <button
            type="button"
            onClick={() => root && browseTo(root, '')}
            className={cn(
              'shrink-0 px-1.5 py-0.5 rounded text-xs font-mono',
              'transition-colors hover:bg-muted/60 hover:text-foreground',
              pathSegments.length === 0
                ? 'text-foreground font-medium'
                : 'text-muted-foreground',
            )}
          >
            {rootLabel(root) || '~'}
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
                  onClick={() => browseTo(root, crumbPath)}
                  className={cn(
                    'px-1.5 py-0.5 rounded text-xs font-mono whitespace-nowrap',
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
            'flex items-center justify-center w-8 h-8 rounded-lg shrink-0',
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
          className="w-full bg-transparent text-base sm:text-sm font-mono focus-visible:outline-none text-foreground placeholder:text-muted-foreground/30 caret-accent"
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
      <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain">
        {loading && (
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

        {!loading && blockingErrorMessage && (
          <div className="flex items-center justify-center h-36 px-6 text-center">
            <p className="text-xs text-destructive leading-relaxed">
              {blockingErrorMessage}
            </p>
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
                  <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/40 select-none">
                    Recent
                  </span>
                </div>
                {recentPaths.map((p) => (
                  <button
                    type="button"
                    key={p}
                    onClick={() => void handleSelectRecent(p)}
                    className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left transition-colors hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
                  >
                    <Clock className="h-3.5 w-3.5 shrink-0 text-muted-foreground/35 group-hover:text-accent/60 transition-colors" />
                    <span className="truncate text-xs font-mono text-muted-foreground group-hover:text-foreground/80 transition-colors">
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
                  <span className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/40 select-none">
                    Roots
                  </span>
                </div>
                {roots.map((workspaceRoot) => (
                  <button
                    type="button"
                    key={workspaceRoot}
                    onClick={() => browseTo(workspaceRoot, '')}
                    className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left transition-colors hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
                  >
                    <FolderOpen className="h-3.5 w-3.5 shrink-0 text-muted-foreground/35 group-hover:text-accent/60 transition-colors" />
                    <span className="truncate text-xs font-mono text-muted-foreground group-hover:text-foreground/80 transition-colors">
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
                onClick={() => browseTo(root, entry.path)}
                className="group flex items-center gap-3 w-full min-h-[44px] px-4 py-2 text-left transition-colors hover:bg-muted/50 active:bg-muted/70 focus-visible:outline-none focus-visible:bg-muted/50"
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
          title={current}
        >
          {current || '—'}
        </span>
        <button
          type="button"
          onClick={handleSelect}
          disabled={!current}
          className={cn(
            'shrink-0 px-3.5 h-7 rounded-md text-xs font-semibold',
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
    </>
  )

  const handleOpenChange = (next: boolean) => {
    if (!next) onClose()
  }

  const initialFocus = shouldAutoFocusTextInputOnOpen(openedWithKeyboard)
    ? inputRef
    : undefined

  if (isDesktop) {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent
          showCloseButton={false}
          initialFocus={initialFocus}
          className="flex flex-col gap-0 p-0 w-110 max-w-[calc(100vw-2rem)] max-h-130 sm:max-w-[calc(100vw-2rem)]"
        >
          <DialogTitle className="sr-only">Select workspace</DialogTitle>
          {body}
        </DialogContent>
      </Dialog>
    )
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        initialFocus={initialFocus}
        className="flex flex-col gap-0 p-0 max-h-[82vh] rounded-t-2xl"
      >
        <SheetTitle className="sr-only">Select workspace</SheetTitle>
        {body}
      </SheetContent>
    </Sheet>
  )
}
