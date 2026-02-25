/**
 * Sidebar component with chat history and settings.
 * Terminal-luxe aesthetic: compact, warm amber accents.
 */

import { FolderOpen, History, Laptop, Moon, Plus, Settings, Sun, Terminal, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { cn } from '../utils/cn'
import { MODEL_PRESETS } from '../utils/storage'
import { Button } from './UI/Button'
import { WorkspacePicker } from './WorkspacePicker'

/** Shared select styling */
const SELECT_CLASS =
  'w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono text-foreground outline-none focus:ring-1 focus:ring-accent/50 focus:border-accent/50 disabled:opacity-50 transition-all'

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
  remoteConfig,
  theme,
  setTheme,
}) {
  const [tab, setTab] = useState('chat')
  const [pickerOpen, setPickerOpen] = useState(false)

  const handleWorkspaceSelect = (cwd) => {
    onUpdateConfig({ ...config, cwd })
  }

  const handleProviderChange = (providerName) => {
    const providerInfo = remoteConfig?.providers?.[providerName]
    const firstModel = providerInfo?.models?.[0] || ''
    onUpdateConfig({ ...config, provider: providerName, model: firstModel, apiBase: '', apiKey: '' })
  }

  const activeProviderInfo = remoteConfig?.providers?.[config.provider]
  const providerModels = activeProviderInfo?.models || []
  const allModelOptions = providerModels.length > 0 ? providerModels : MODEL_PRESETS

  return (
    <div className={cn('flex w-60 flex-col border-r border-border/60 bg-sidebar-bg', className)}>
      {/* Header */}
      <div className="flex h-14 shrink-0 items-center px-4 border-b border-border/40">
        <div className="flex items-center gap-2.5">
          <Terminal className="h-4 w-4 text-accent" />
          <span className="font-display text-sm tracking-tight text-foreground">mycode</span>
        </div>
      </div>

      {/* Navigation Tabs */}
      <div className="px-3 py-3 shrink-0">
        <div className="flex gap-1">
          <button
            type="button"
            onClick={() => setTab('chat')}
            className={cn(
              'flex-1 flex items-center justify-center rounded-md py-1.5 text-xs font-medium transition-all',
              tab === 'chat' ? 'bg-secondary text-foreground' : 'text-muted-foreground hover:text-foreground'
            )}
          >
            <History className="mr-1.5 h-3 w-3" />
            History
          </button>
          <button
            type="button"
            onClick={() => setTab('settings')}
            className={cn(
              'flex-1 flex items-center justify-center rounded-md py-1.5 text-xs font-medium transition-all',
              tab === 'settings' ? 'bg-secondary text-foreground' : 'text-muted-foreground hover:text-foreground'
            )}
          >
            <Settings className="mr-1.5 h-3 w-3" />
            Settings
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        {/* Chat Sessions List */}
        {tab === 'chat' && (
          <div className="flex h-full flex-col px-3">
            <div className="pb-3 shrink-0">
              <button
                type="button"
                onClick={onCreateSession}
                className="w-full flex items-center gap-2 rounded-md border border-dashed border-border/60 px-3 py-2 text-xs font-medium text-muted-foreground hover:text-accent hover:border-accent/40 transition-all"
              >
                <Plus className="h-3 w-3" />
                New Chat
              </button>
            </div>
            <div className="flex-1 overflow-y-auto space-y-0.5 pb-4">
              {sessions.map((session) => (
                <button
                  type="button"
                  key={session.id}
                  className={cn(
                    'group relative flex w-full items-center justify-between rounded-md px-3 py-2 text-xs cursor-pointer transition-all text-left',
                    activeSession?.id === session.id
                      ? 'bg-secondary text-foreground'
                      : 'text-muted-foreground hover:text-foreground hover:bg-secondary/50'
                  )}
                  onClick={() => onSelectSession(session.id)}
                >
                  {activeSession?.id === session.id && (
                    <div className="absolute left-0 top-1/2 -translate-y-1/2 h-4 w-0.5 rounded-r bg-accent" />
                  )}
                  <span className="truncate flex-1 pl-1">{session.title || 'New Chat'}</span>
                  {activeSession?.id !== session.id && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-5 w-5 opacity-0 group-hover:opacity-100 transition-opacity"
                      onClick={(e) => {
                        e.stopPropagation()
                        onDeleteSession(session.id)
                      }}
                    >
                      <Trash2 className="h-3 w-3 text-muted-foreground hover:text-destructive" />
                    </Button>
                  )}
                </button>
              ))}
              {sessions.length === 0 && (
                <div className="py-12 text-center text-xs text-muted-foreground/60 flex flex-col items-center gap-2">
                  <History className="h-4 w-4 opacity-30" />
                  No history
                </div>
              )}
            </div>
          </div>
        )}

        {/* Settings Panel */}
        {tab === 'settings' && (
          <div className="h-full overflow-y-auto px-3 pb-6 space-y-3">
            {/* Theme */}
            <div className="space-y-2 p-3 rounded-md bg-secondary/50">
              <div className="flex items-center gap-2 text-2xs font-mono font-medium text-muted-foreground uppercase tracking-widest">
                Appearance
              </div>
              <div className="grid grid-cols-3 gap-1">
                {[
                  { key: 'light', icon: Sun, label: 'Light' },
                  { key: 'dark', icon: Moon, label: 'Dark' },
                  { key: 'system', icon: Laptop, label: 'Auto' },
                ].map(({ key, icon: Icon }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setTheme(key)}
                    className={cn(
                      'flex items-center justify-center rounded py-1.5 transition-all text-xs',
                      theme === key
                        ? 'bg-accent text-accent-foreground'
                        : 'text-muted-foreground hover:text-foreground hover:bg-secondary'
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                  </button>
                ))}
              </div>
            </div>

            {/* Workspace */}
            <div className="space-y-2 p-3 rounded-md bg-secondary/50">
              <div className="flex items-center justify-between">
                <span className="text-2xs font-mono font-medium text-muted-foreground uppercase tracking-widest">
                  Workspace
                </span>
                <button
                  type="button"
                  onClick={() => setPickerOpen(true)}
                  className="text-muted-foreground hover:text-accent transition-colors"
                >
                  <FolderOpen className="h-3 w-3" />
                </button>
              </div>
              <div className="rounded bg-background/50 px-2.5 py-2">
                <p
                  className="break-all font-mono text-2xs leading-relaxed text-muted-foreground"
                  title={config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd}
                >
                  {config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd}
                </p>
              </div>
            </div>

            {/* Provider */}
            <div className="space-y-3 p-3 rounded-md bg-secondary/50">
              <div className="text-2xs font-mono font-medium text-muted-foreground uppercase tracking-widest">
                Provider
              </div>
              <div className="space-y-3">
                {remoteConfig?.providers && Object.keys(remoteConfig.providers).length > 0 && (
                  <div className="space-y-1.5">
                    <label htmlFor="provider-select" className="text-2xs font-mono text-muted-foreground/70">
                      provider
                    </label>
                    <select
                      id="provider-select"
                      value={config.provider || ''}
                      onChange={(e) => handleProviderChange(e.target.value)}
                      className={SELECT_CLASS}
                    >
                      <option value="">select...</option>
                      {Object.values(remoteConfig.providers).map((p) => (
                        <option key={p.name} value={p.name}>
                          {p.name} ({p.type})
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                {providerModels.length > 0 ? (
                  <div className="space-y-1.5">
                    <label htmlFor="model-input" className="text-2xs font-mono text-muted-foreground/70">
                      model
                    </label>
                    <select
                      id="model-input"
                      value={config.model || ''}
                      onChange={(e) => onUpdateConfig({ ...config, model: e.target.value })}
                      className={SELECT_CLASS}
                    >
                      {providerModels.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : (
                  <div className="text-center text-2xs text-muted-foreground/50 py-2">No models available</div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Workspace Picker Modal */}
      <WorkspacePicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        currentCwd={config.cwd}
        cwdHistory={cwdHistory}
        onSelect={handleWorkspaceSelect}
      />
    </div>
  )
}
