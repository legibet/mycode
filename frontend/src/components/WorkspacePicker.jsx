/**
 * Workspace folder picker modal.
 * Allows browsing and selecting a working directory.
 */

import { useCallback, useEffect, useState } from 'react'
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
  const relative = normPath.startsWith(normRoot) ? normPath.slice(normRoot.length) : normPath
  return relative.replace(/^\/+/, '')
}

const rootLabel = (value) => {
  if (!value || value === '/' || value === '\\') return 'Root'
  const normalized = value.replace(/[\\/]+$/, '')
  if (/\/Users\/[^/]+$/.test(normalized) || /\/home\/[^/]+$/.test(normalized)) return 'Home'
  const parts = normalized.split(/[/\\]/)
  return parts[parts.length - 1] || value
}

export function WorkspacePicker({ open, onClose, currentCwd, cwdHistory, onSelect }) {
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
          setState((prev) => ({ ...prev, roots: [], loading: false, error: 'No workspace roots found' }))
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
        if (active) setState((prev) => ({ ...prev, loading: false, error: e.message }))
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
    ? state.entries.filter((e) => e.name.toLowerCase().includes(filter.toLowerCase()))
    : state.entries

  if (!open) return null

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-3xl rounded-lg border border-input bg-background shadow-lg">
        {/* Header */}
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div>
            <div className="text-sm font-semibold">Select workspace folder</div>
            <p className="text-[10px] text-muted-foreground">Pick any folder on the server</p>
          </div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>

        {/* Body */}
        <div className="grid gap-4 p-4 md:grid-cols-[180px_1fr]">
          {/* Left: Places & Recent */}
          <div className="space-y-4">
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Places</div>
              <div className="space-y-1">
                {state.roots.map((root) => (
                  <Button
                    key={root}
                    variant={root === state.root ? 'secondary' : 'outline'}
                    size="sm"
                    onClick={() => browsePath(root, '')}
                    className="w-full justify-start"
                  >
                    <span className="truncate">{rootLabel(root)}</span>
                  </Button>
                ))}
              </div>
            </div>
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Recent</div>
              <div className="space-y-1">
                {(cwdHistory || []).length === 0 && (
                  <span className="text-[10px] text-muted-foreground">No recent folders</span>
                )}
                {(cwdHistory || []).map((item) => (
                  <Button
                    key={item}
                    variant="ghost"
                    size="sm"
                    onClick={() => goToPath(item)}
                    className="w-full justify-start"
                  >
                    <span className="truncate">{item}</span>
                  </Button>
                ))}
              </div>
            </div>
          </div>

          {/* Right: Browser */}
          <div className="space-y-3">
            {/* Path input */}
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Path</div>
              <div className="flex gap-2">
                <Input
                  value={pathInput}
                  onChange={(e) => setPathInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && goToPath(pathInput)}
                  placeholder="/path/to/folder"
                  className="font-mono text-xs"
                />
                <Button size="sm" variant="outline" onClick={() => goToPath(pathInput)}>
                  Go
                </Button>
                <Button size="sm" variant="outline" onClick={handleGoParent} disabled={!state.root}>
                  Up
                </Button>
              </div>

              {/* Breadcrumbs */}
              <div className="flex flex-wrap items-center gap-1 text-xs text-foreground">
                <Button variant="ghost" size="sm" onClick={() => browsePath(state.root, '')} disabled={!state.root}>
                  {rootLabel(state.root || 'root')}
                </Button>
                {pathSegments.map((segment, index) => {
                  const crumbPath = pathSegments.slice(0, index + 1).join('/')
                  return (
                    <div key={crumbPath} className="flex items-center gap-1">
                      <span className="text-muted-foreground">/</span>
                      <Button variant="ghost" size="sm" onClick={() => browsePath(state.root, crumbPath)}>
                        {segment}
                      </Button>
                    </div>
                  )
                })}
              </div>
              <p className="text-[10px] text-muted-foreground break-all">{state.current || 'Select a folder'}</p>
            </div>

            {/* Folder list */}
            <div className="space-y-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Folders</div>
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter folders"
                className="text-xs"
              />
              <p className="text-[10px] text-muted-foreground">Hidden folders are excluded.</p>
              <div className="max-h-56 overflow-y-auto rounded-md border border-input bg-background/70">
                {state.loading && <div className="px-3 py-2 text-xs text-muted-foreground">Loading...</div>}
                {!state.loading && state.error && (
                  <div className="px-3 py-2 text-xs text-destructive">{state.error}</div>
                )}
                {!state.loading && !state.error && filteredEntries.length === 0 && (
                  <div className="px-3 py-2 text-xs text-muted-foreground">No folders</div>
                )}
                {!state.loading &&
                  !state.error &&
                  filteredEntries.map((entry) => (
                    <button
                      type="button"
                      key={entry.path}
                      className="flex w-full items-center px-3 py-2 text-left text-xs hover:bg-muted/60 transition-colors"
                      onClick={() => browsePath(state.root, entry.path)}
                    >
                      <span className="truncate">{entry.name}</span>
                    </button>
                  ))}
              </div>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t px-4 py-3">
          <Button size="sm" onClick={handleSelect} disabled={!state.current}>
            Use this folder
          </Button>
          <div className="text-[10px] text-muted-foreground">Apply to current chat</div>
        </div>
      </div>
    </div>
  )
}
