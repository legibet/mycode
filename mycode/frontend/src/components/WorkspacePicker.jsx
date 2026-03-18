/**
 * Workspace folder picker modal.
 * Allows browsing and selecting a working directory.
 */

import { CornerUpLeft, Folder, Search } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '../utils/cn'
import { Button } from './UI/Button'
import { Input } from './UI/Input'

const normalizeSlashes = (value) => value.replace(/\\/g, '/')
const isAbsolutePath = (value) => /^([a-zA-Z]:[\\/]|\/)/.test(value)

const matchRoot = (roots, value) => {
  const normalized = normalizeSlashes(value)
  const sorted = [...roots].sort((a, b) => b.length - a.length)
  return (
    sorted.find((root) => {
      const normRoot = normalizeSlashes(root).replace(/\/+$/, '')
      return normalized === normRoot || normalized.startsWith(`${normRoot}/`)
    }) || roots[0]
  )
}

const toRelativePath = (root, absolutePath) => {
  const normRoot = normalizeSlashes(root).replace(/\/+$/, '')
  const normPath = normalizeSlashes(absolutePath)
  if (normPath === normRoot) return ''
  const relative = normPath.startsWith(normRoot)
    ? normPath.slice(normRoot.length)
    : normPath
  return relative.replace(/^\/+/, '')
}

const rootLabel = (value) => {
  if (!value || value === '/' || value === '\\') return 'Root'
  const normalized = value.replace(/[\\/]+$/, '')
  if (/\/Users\/[^/]+$/.test(normalized) || /\/home\/[^/]+$/.test(normalized))
    return 'Home'
  const parts = normalized.split(/[/\\]/)
  return parts[parts.length - 1] || value
}

export function WorkspacePicker({ open, onClose, currentCwd, onSelect }) {
  const [state, setState] = useState({
    roots: [],
    root: '',
    path: '',
    current: '',
    entries: [],
    loading: false,
    error: '',
  })
  const [pathInput, setPathInput] = useState('')
  const [filter, setFilter] = useState('')

  const loadRoots = useCallback(async () => {
    const res = await fetch('/api/workspaces/roots')
    if (!res.ok) throw new Error('Failed to load roots')
    const data = await res.json()
    return data.roots || []
  }, [])

  const browsePath = useCallback(async (root, path = '') => {
    setState((prev) => ({ ...prev, loading: true, error: '' }))
    try {
      const params = new URLSearchParams({ root })
      if (path) params.set('path', path)
      const res = await fetch(`/api/workspaces/browse?${params.toString()}`)
      if (!res.ok) throw new Error('Failed to browse directory')
      const data = await res.json()
      if (data.error) throw new Error(data.error)
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
      setState((prev) => ({ ...prev, loading: false, error: e.message }))
    }
  }, [])

  // Initialize on open
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
            error: 'No workspace roots found',
          }))
          return
        }
        setFilter('')
        setPathInput('')
        setState((prev) => ({ ...prev, roots }))

        if (currentCwd) {
          const root = matchRoot(roots, currentCwd)
          await browsePath(root, toRelativePath(root, currentCwd))
        } else {
          await browsePath(roots[0], '')
        }
      } catch (e) {
        if (active)
          setState((prev) => ({ ...prev, loading: false, error: e.message }))
      }
    }
    init()
    return () => {
      active = false
    }
  }, [open, browsePath, currentCwd, loadRoots])

  const goToPath = async (value) => {
    const trimmed = value.trim()
    if (!trimmed || !state.roots.length) return

    if (isAbsolutePath(trimmed)) {
      const root = matchRoot(state.roots, trimmed)
      if (!root) {
        setState((prev) => ({ ...prev, error: 'No matching root for path' }))
        return
      }
      await browsePath(root, toRelativePath(root, trimmed))
    } else {
      const base = state.path ? `${state.path}/${trimmed}` : trimmed
      await browsePath(state.root, base)
    }
  }

  const handleGoParent = async () => {
    if (!state.root) return
    if (!state.path) {
      await browsePath(state.root, '')
      return
    }
    const segments = state.path.split('/')
    await browsePath(state.root, segments.slice(0, -1).join('/'))
  }

  const handleSelect = () => {
    if (state.current) {
      onSelect(state.current)
      onClose()
    }
  }

  const pathSegments = state.path ? state.path.split('/') : []
  const filteredEntries = filter
    ? state.entries.filter((e) =>
        e.name.toLowerCase().includes(filter.toLowerCase()),
      )
    : state.entries

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-background/80 backdrop-blur-sm transition-opacity"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Modal Dialog */}
      <div className="relative flex max-h-[85vh] h-[650px] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-border/50 bg-background shadow-2xl">
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-border/50 bg-muted/20 px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold tracking-tight">
              Select Workspace
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              Browse your file system and select a working directory
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            className="hidden sm:inline-flex"
          >
            Cancel
          </Button>
        </div>

        {/* Layout Body */}
        <div className="flex flex-1 min-h-0 bg-background">
          <div className="flex flex-1 flex-col min-w-0">
            {/* Toolbar Area */}
            <div className="shrink-0 space-y-4 border-b border-border/50 p-6 bg-muted/5">
              {/* Path Input Box */}
              <div className="flex gap-2">
                <Button
                  size="icon"
                  variant="outline"
                  className="h-10 w-10 shrink-0 border-border/50 shadow-sm"
                  onClick={handleGoParent}
                  disabled={!state.root}
                  title="Go Up"
                >
                  <CornerUpLeft className="h-4 w-4" />
                </Button>
                <div className="relative flex-1 group">
                  <Input
                    value={pathInput}
                    onChange={(e) => setPathInput(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && goToPath(pathInput)}
                    placeholder="Type an absolute or relative path..."
                    className="h-10 pr-16 font-mono text-sm border-border/50 bg-background focus-visible:ring-primary/20 shadow-sm transition-all"
                  />
                  <div className="absolute inset-y-0 right-1 flex items-center pr-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => goToPath(pathInput)}
                      className="h-7 text-xs font-semibold hover:bg-muted text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity"
                    >
                      GO
                    </Button>
                  </div>
                </div>
              </div>

              {/* Breadcrumbs */}
              <div className="flex h-6 flex-wrap items-center gap-1.5 overflow-hidden text-sm">
                <select
                  value={state.root || ''}
                  onChange={(e) => browsePath(e.target.value, '')}
                  disabled={!state.root || state.roots.length <= 1}
                  className={cn(
                    'bg-transparent font-medium focus:outline-none cursor-pointer max-w-[150px] truncate outline-none',
                    state.roots.length > 1
                      ? 'hover:text-primary text-muted-foreground'
                      : 'text-muted-foreground appearance-none',
                  )}
                  title={state.root}
                >
                  {state.roots.map((root) => (
                    <option key={root} value={root}>
                      {rootLabel(root)}
                    </option>
                  ))}
                </select>
                {pathSegments.map((segment, index) => {
                  const crumbPath = pathSegments.slice(0, index + 1).join('/')
                  return (
                    <div key={crumbPath} className="flex items-center gap-1.5">
                      <span className="text-muted-foreground/40 font-semibold">
                        /
                      </span>
                      <button
                        type="button"
                        onClick={() => browsePath(state.root, crumbPath)}
                        className={cn(
                          'transition-colors hover:underline',
                          index === pathSegments.length - 1
                            ? 'text-foreground font-semibold'
                            : 'hover:text-primary text-muted-foreground font-medium',
                        )}
                      >
                        {segment}
                      </button>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Folder List Window */}
            <div className="flex flex-1 flex-col overflow-hidden">
              <div className="flex items-center justify-between border-b border-border/50 bg-muted/10 px-6 py-2">
                <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                  Directories
                </div>
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground/50" />
                  <Input
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    placeholder="Filter..."
                    className="h-7 w-48 pl-8 text-xs bg-background/50 border-transparent hover:border-border/50 focus:border-border/50 focus:ring-0 shadow-none transition-all"
                  />
                </div>
              </div>

              <div className="flex-1 overflow-y-auto p-3 outline-none">
                {state.loading && (
                  <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                    Loading contents...
                  </div>
                )}
                {!state.loading && state.error && (
                  <div className="flex h-full items-center justify-center text-sm text-destructive">
                    {state.error}
                  </div>
                )}
                {!state.loading &&
                  !state.error &&
                  filteredEntries.length === 0 && (
                    <div className="flex flex-col h-full items-center justify-center text-muted-foreground gap-3">
                      <Folder className="h-10 w-10 opacity-20" />
                      <p className="text-sm">This folder is empty.</p>
                    </div>
                  )}

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {!state.loading &&
                    !state.error &&
                    filteredEntries.map((entry) => (
                      <button
                        type="button"
                        key={entry.path}
                        onClick={() => browsePath(state.root, entry.path)}
                        className="group flex items-center gap-3 rounded-xl border border-transparent p-3 text-left transition-all hover:border-border/50 hover:bg-muted/30 focus:outline-none focus:ring-2 focus:ring-primary/20"
                      >
                        <Folder className="h-5 w-5 fill-muted-foreground/20 text-muted-foreground group-hover:fill-primary/20 group-hover:text-primary transition-colors" />
                        <span className="truncate text-sm font-medium text-foreground/90 group-hover:text-foreground">
                          {entry.name}
                        </span>
                      </button>
                    ))}
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Footer Actions */}
        <div className="flex shrink-0 items-center justify-between border-t border-border/50 bg-muted/10 px-6 py-4">
          <div className="flex items-center gap-2 max-w-[50%]">
            <span className="text-xs text-muted-foreground">
              Selected target:
            </span>
            <span className="text-xs font-mono font-medium truncate bg-background px-2 py-1 rounded-md border border-border/50 shadow-sm">
              {state.current || 'None'}
            </span>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose} className="sm:hidden">
              Cancel
            </Button>
            <Button
              onClick={handleSelect}
              disabled={!state.current}
              className="px-8 shadow-sm font-semibold rounded-lg"
            >
              Open Workspace
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
