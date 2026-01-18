import { Code2, FolderOpen, History, Laptop, MessageSquarePlus, Moon, Settings, Sun, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { cn } from '../utils/cn'
import { MODEL_PRESETS } from '../utils/storage'
import { Button } from './UI/Button'
import { Input } from './UI/Input'

const normalizeSlashes = (value) => value.replace(/\\/g, '/')

const isAbsolutePath = (value) => /^([a-zA-Z]:[\\/]|\/)/.test(value)

const matchRoot = (roots, value) => {
  const normalizedValue = normalizeSlashes(value)
  const sorted = [...roots].sort((a, b) => b.length - a.length)
  return (
    sorted.find((root) => {
      const normalizedRoot = normalizeSlashes(root).replace(/\/+$/, '')
      if (normalizedValue === normalizedRoot) return true
      return normalizedValue.startsWith(`${normalizedRoot}/`)
    }) || roots[0]
  )
}

const toRelativePath = (root, absolutePath) => {
  const normalizedRoot = normalizeSlashes(root).replace(/\/+$/, '')
  const normalizedPath = normalizeSlashes(absolutePath)
  if (normalizedPath === normalizedRoot) return ''
  let relative = normalizedPath.startsWith(normalizedRoot)
    ? normalizedPath.slice(normalizedRoot.length)
    : normalizedPath
  relative = relative.replace(/^\/+/, '')
  return relative
}

export function Sidebar({
  className,
  sessions,
  activeSession,
  onSelectSession,
  onCreateSession,
  onDeleteSession,
  config,
  onUpdateConfig,
  cwdHistory,
  theme,
  setTheme,
}) {
  const [tab, setTab] = useState('chat') // 'chat' | 'settings'
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerState, setPickerState] = useState({
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
    const response = await fetch('/api/workspaces/roots')
    if (!response.ok) throw new Error('Failed to load roots')
    const data = await response.json()
    return data.roots || []
  }, [])

  const browsePath = useCallback(async (root, path = '') => {
    setPickerState((prev) => ({ ...prev, loading: true, error: '' }))
    try {
      const params = new URLSearchParams({ root })
      if (path) params.set('path', path)
      const response = await fetch(`/api/workspaces/browse?${params.toString()}`)
      if (!response.ok) throw new Error('Failed to browse directory')
      const data = await response.json()
      if (data.error) throw new Error(data.error)
      setPickerState((prev) => ({
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
      setPickerState((prev) => ({
        ...prev,
        loading: false,
        error: e.message || 'Failed to browse directory',
      }))
    }
  }, [])

  useEffect(() => {
    if (!pickerOpen) return
    let active = true
    const init = async () => {
      try {
        const roots = await loadRoots()
        if (!active) return
        if (!roots.length) {
          setPickerState((prev) => ({ ...prev, roots: [], loading: false, error: 'No workspace roots found' }))
          return
        }
        setFilter('')
        setPathInput('')
        setPickerState((prev) => ({ ...prev, roots }))
        if (config.cwd) {
          const root = matchRoot(roots, config.cwd)
          const relative = toRelativePath(root, config.cwd)
          await browsePath(root, relative)
          return
        }
        await browsePath(roots[0], '')
      } catch (e) {
        if (!active) return
        setPickerState((prev) => ({
          ...prev,
          loading: false,
          error: e.message || 'Failed to load workspace roots',
        }))
      }
    }
    init()
    return () => {
      active = false
    }
  }, [pickerOpen, browsePath, config.cwd, loadRoots])

  const handleUseCurrent = () => {
    if (!pickerState.current) return
    onUpdateConfig({ ...config, cwd: pickerState.current })
    setPickerOpen(false)
  }

  const rootLabel = (value) => {
    if (!value) return 'Root'
    if (value === '/' || value === '\\') return 'Root'
    const normalized = value.replace(/[\\/]+$/, '')
    if (/\/Users\/[^/]+$/.test(normalized) || /\/home\/[^/]+$/.test(normalized)) return 'Home'
    const parts = normalized.split(/[/\\]/)
    return parts[parts.length - 1] || value
  }

  const pathSegments = pickerState.path ? pickerState.path.split('/') : []

  const matchRootForPath = (value) => {
    const roots = pickerState.roots || []
    return matchRoot(roots, value)
  }

  const goToPath = async (value) => {
    const trimmed = value.trim()
    if (!trimmed) return
    const roots = pickerState.roots || []
    if (!roots.length) return
    if (isAbsolutePath(trimmed)) {
      const root = matchRootForPath(trimmed)
      if (!root) {
        setPickerState((prev) => ({ ...prev, error: 'No matching root for path' }))
        return
      }
      await browsePath(root, toRelativePath(root, trimmed))
      return
    }
    const base = pickerState.path ? `${pickerState.path}/${trimmed}` : trimmed
    await browsePath(pickerState.root, base)
  }

  const handleGoPath = async () => {
    const value = pathInput.trim()
    if (!value) return
    await goToPath(value)
  }

  const handleGoParent = async () => {
    if (!pickerState.root) return
    if (!pickerState.path) {
      await browsePath(pickerState.root, '')
      return
    }
    const parent = pathSegments.slice(0, -1).join('/')
    await browsePath(pickerState.root, parent)
  }

  const filteredEntries = filter
    ? pickerState.entries.filter((entry) => entry.name.toLowerCase().includes(filter.toLowerCase()))
    : pickerState.entries

  return (
    <div className={cn('flex w-64 flex-col border-r bg-muted/10', className)}>
      {/* Header */}
      <div className="flex h-14 items-center px-4 border-b">
        <div className="flex items-center gap-2 font-semibold">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Code2 className="h-5 w-5" />
          </div>
          <span>mycode</span>
        </div>
      </div>

      {/* Navigation Tabs */}
      <div className="flex p-2 gap-1">
        <Button
          variant={tab === 'chat' ? 'secondary' : 'ghost'}
          size="sm"
          onClick={() => setTab('chat')}
          className="flex-1 justify-center"
        >
          <History className="mr-2 h-4 w-4" />
          History
        </Button>
        <Button
          variant={tab === 'settings' ? 'secondary' : 'ghost'}
          size="sm"
          onClick={() => setTab('settings')}
          className="flex-1 justify-center"
        >
          <Settings className="mr-2 h-4 w-4" />
          Settings
        </Button>
      </div>

      <div className="flex-1 overflow-hidden">
        {/* Chat Sessions List */}
        {tab === 'chat' && (
          <div className="flex h-full flex-col">
            <div className="p-2">
              <Button onClick={onCreateSession} className="w-full justify-start" variant="outline">
                <MessageSquarePlus className="mr-2 h-4 w-4" />
                New Chat
              </Button>
            </div>
            <div className="flex-1 overflow-y-auto px-2 space-y-1">
              {sessions.map((session) => (
                <button
                  type="button"
                  key={session.id}
                  className={cn(
                    'group flex w-full items-center justify-between rounded-md px-2 py-2 text-sm hover:bg-muted cursor-pointer transition-colors text-left',
                    activeSession?.id === session.id && 'bg-muted font-medium'
                  )}
                  onClick={() => onSelectSession(session.id)}
                >
                  <span className="truncate flex-1">{session.title || 'New Chat'}</span>
                  {activeSession?.id === session.id ? (
                    <div className="h-2 w-2 rounded-full bg-primary shrink-0" />
                  ) : (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={(e) => {
                        e.stopPropagation()
                        onDeleteSession(session.id)
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-muted-foreground hover:text-destructive" />
                    </Button>
                  )}
                </button>
              ))}
              {sessions.length === 0 && (
                <div className="px-4 py-8 text-center text-xs text-muted-foreground">No chat history</div>
              )}
            </div>
          </div>
        )}

        {/* Settings Panel */}
        {tab === 'settings' && (
          <div className="h-full overflow-y-auto p-4 space-y-6">
            {/* Theme */}
            <div className="space-y-3">
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Theme</span>
              <div className="grid grid-cols-3 gap-2">
                <Button
                  variant={theme === 'light' ? 'secondary' : 'outline'}
                  size="sm"
                  onClick={() => setTheme('light')}
                  className="w-full"
                >
                  <Sun className="h-4 w-4" />
                </Button>
                <Button
                  variant={theme === 'dark' ? 'secondary' : 'outline'}
                  size="sm"
                  onClick={() => setTheme('dark')}
                  className="w-full"
                >
                  <Moon className="h-4 w-4" />
                </Button>
                <Button
                  variant={theme === 'system' ? 'secondary' : 'outline'}
                  size="sm"
                  onClick={() => setTheme('system')}
                  className="w-full"
                >
                  <Laptop className="h-4 w-4" />
                </Button>
              </div>
            </div>

            {/* Workspace */}
            <div className="space-y-3">
              <div className="text-xs font-medium text-muted-foreground uppercase tracking-wider">Workspace</div>
              <div className="space-y-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Current path</div>
                    <p className="mt-1 break-all font-mono text-xs text-foreground">{config.cwd}</p>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setPickerOpen((open) => !open)}
                    className="shrink-0"
                  >
                    <FolderOpen className="mr-2 h-4 w-4" />
                    {pickerOpen ? 'Close' : 'Choose'}
                  </Button>
                </div>
              </div>
              {pickerOpen && (
                <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
                  <div className="w-full max-w-3xl rounded-lg border border-input bg-background shadow-lg">
                    <div className="flex items-center justify-between border-b px-4 py-3">
                      <div>
                        <div className="text-sm font-semibold">Select workspace folder</div>
                        <p className="text-[10px] text-muted-foreground">Pick any folder on the server</p>
                      </div>
                      <Button size="sm" variant="ghost" onClick={() => setPickerOpen(false)}>
                        Close
                      </Button>
                    </div>
                    <div className="grid gap-4 p-4 md:grid-cols-[180px_1fr]">
                      <div className="space-y-4">
                        <div className="space-y-2">
                          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Places</div>
                          <div className="space-y-1">
                            {(pickerState.roots || []).map((root) => (
                              <Button
                                key={root}
                                variant={root === pickerState.root ? 'secondary' : 'outline'}
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
                      <div className="space-y-3">
                        <div className="space-y-2">
                          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Path</div>
                          <div className="flex gap-2">
                            <Input
                              value={pathInput}
                              onChange={(e) => setPathInput(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') handleGoPath()
                              }}
                              placeholder="/path/to/folder"
                              className="font-mono text-xs"
                            />
                            <Button size="sm" variant="outline" onClick={handleGoPath}>
                              Go
                            </Button>
                            <Button size="sm" variant="outline" onClick={handleGoParent} disabled={!pickerState.root}>
                              Up
                            </Button>
                          </div>
                          <div className="flex flex-wrap items-center gap-1 text-xs text-foreground">
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => browsePath(pickerState.root, '')}
                              disabled={!pickerState.root}
                            >
                              {rootLabel(pickerState.root || 'root')}
                            </Button>
                            {pathSegments.map((segment, index) => {
                              const crumbPath = pathSegments.slice(0, index + 1).join('/')
                              return (
                                <div key={crumbPath} className="flex items-center gap-1">
                                  <span className="text-muted-foreground">/</span>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => browsePath(pickerState.root, crumbPath)}
                                  >
                                    {segment}
                                  </Button>
                                </div>
                              )
                            })}
                          </div>
                          <p className="text-[10px] text-muted-foreground break-all">
                            {pickerState.current || 'Select a folder'}
                          </p>
                        </div>
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
                            {pickerState.loading && (
                              <div className="px-3 py-2 text-xs text-muted-foreground">Loading...</div>
                            )}
                            {!pickerState.loading && pickerState.error && (
                              <div className="px-3 py-2 text-xs text-destructive">{pickerState.error}</div>
                            )}
                            {!pickerState.loading && !pickerState.error && filteredEntries.length === 0 && (
                              <div className="px-3 py-2 text-xs text-muted-foreground">No folders</div>
                            )}
                            {!pickerState.loading &&
                              !pickerState.error &&
                              filteredEntries.map((entry) => (
                                <button
                                  type="button"
                                  key={entry.path}
                                  className="flex w-full items-center px-3 py-2 text-left text-xs hover:bg-muted/60 transition-colors"
                                  onClick={() => browsePath(pickerState.root, entry.path)}
                                >
                                  <span className="truncate">{entry.name}</span>
                                </button>
                              ))}
                          </div>
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center justify-between border-t px-4 py-3">
                      <Button size="sm" onClick={handleUseCurrent} disabled={!pickerState.current}>
                        Use this folder
                      </Button>
                      <div className="text-[10px] text-muted-foreground">Apply to current chat</div>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* API Configuration */}
            <div className="space-y-3">
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">LLM Provider</span>
              <div className="space-y-3">
                <div className="space-y-1">
                  <label htmlFor="model-input" className="text-xs text-muted-foreground">
                    Model
                  </label>
                  <Input
                    id="model-input"
                    value={config.model}
                    onChange={(e) => onUpdateConfig({ ...config, model: e.target.value })}
                    placeholder="provider:model"
                    list="model-presets"
                    className="font-mono text-xs"
                  />
                  <datalist id="model-presets">
                    {MODEL_PRESETS.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                </div>

                <div className="space-y-1">
                  <label htmlFor="api-key-input" className="text-xs text-muted-foreground">
                    API Key
                  </label>
                  <Input
                    id="api-key-input"
                    type="password"
                    value={config.apiKey || ''}
                    onChange={(e) => onUpdateConfig({ ...config, apiKey: e.target.value })}
                    placeholder="sk-..."
                    className="font-mono text-xs"
                  />
                </div>

                <div className="space-y-1">
                  <label htmlFor="api-base-input" className="text-xs text-muted-foreground">
                    Base URL (Optional)
                  </label>
                  <Input
                    id="api-base-input"
                    value={config.apiBase || ''}
                    onChange={(e) => onUpdateConfig({ ...config, apiBase: e.target.value })}
                    placeholder="https://api.example.com/v1"
                    className="font-mono text-xs"
                  />
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
