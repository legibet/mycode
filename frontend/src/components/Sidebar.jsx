/**
 * Sidebar component with chat history and settings.
 */

import { Code2, FolderOpen, History, Laptop, MessageSquarePlus, Moon, Settings, Sun, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { cn } from '../utils/cn'
import { MODEL_PRESETS } from '../utils/storage'
import { Button } from './UI/Button'
import { Input } from './UI/Input'
import { WorkspacePicker } from './WorkspacePicker'

/** Shared select style matching Input component */
const SELECT_CLASS =
  'w-full rounded-lg border border-input/50 bg-background/50 px-3 py-2 text-sm font-medium text-foreground shadow-sm outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary disabled:opacity-50 transition-all'

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
  remoteConfig, // { providers: { name: { type, models, base_url, has_api_key } }, default: { provider, model } }
  theme,
  setTheme,
}) {
  const [tab, setTab] = useState('chat') // 'chat' | 'settings'
  const [pickerOpen, setPickerOpen] = useState(false)

  const handleWorkspaceSelect = (cwd) => {
    onUpdateConfig({ ...config, cwd })
  }

  // When provider selection changes, reset model to that provider's first model
  const handleProviderChange = (providerName) => {
    const providerInfo = remoteConfig?.providers?.[providerName]
    const firstModel = providerInfo?.models?.[0] || ''
    onUpdateConfig({ ...config, provider: providerName, model: firstModel, apiBase: '', apiKey: '' })
  }

  const activeProviderInfo = remoteConfig?.providers?.[config.provider]
  const providerModels = activeProviderInfo?.models || []

  // Combine provider models with MODEL_PRESETS for the datalist
  const allModelOptions = providerModels.length > 0 ? providerModels : MODEL_PRESETS

  return (
    <div
      className={cn(
        'flex w-72 flex-col border-r border-border/40 bg-zinc-50/50 dark:bg-zinc-950/50 backdrop-blur-xl',
        className
      )}
    >
      {/* Header */}
      <div className="flex h-16 shrink-0 items-center px-5 border-b border-border/40">
        <div className="flex items-center gap-3 font-semibold tracking-tight">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-primary/80 text-primary-foreground shadow-sm">
            <Code2 className="h-4 w-4" />
          </div>
          <span className="text-base text-foreground/90">mycode</span>
        </div>
      </div>

      {/* Navigation Tabs - Segmented Control */}
      <div className="px-4 py-4 shrink-0">
        <div className="flex p-1 bg-muted/50 rounded-lg">
          <button
            type="button"
            onClick={() => setTab('chat')}
            className={cn(
              'flex-1 flex items-center justify-center rounded-md py-1.5 text-xs font-medium transition-all',
              tab === 'chat'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground hover:bg-muted/80'
            )}
          >
            <History className="mr-2 h-3.5 w-3.5" />
            History
          </button>
          <button
            type="button"
            onClick={() => setTab('settings')}
            className={cn(
              'flex-1 flex items-center justify-center rounded-md py-1.5 text-xs font-medium transition-all',
              tab === 'settings'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground hover:bg-muted/80'
            )}
          >
            <Settings className="mr-2 h-3.5 w-3.5" />
            Settings
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        {/* Chat Sessions List */}
        {tab === 'chat' && (
          <div className="flex h-full flex-col px-4">
            <div className="pb-4 shrink-0">
              <Button
                onClick={onCreateSession}
                className="w-full justify-start rounded-xl font-medium bg-primary/5 text-primary hover:bg-primary/10 hover:text-primary focus:bg-primary/10 border-0 shadow-none h-10 transition-all font-sans"
                variant="outline"
              >
                <MessageSquarePlus className="mr-2 h-4 w-4" />
                New Chat
              </Button>
            </div>
            <div className="flex-1 overflow-y-auto space-y-1 -mx-2 px-2 pb-4">
              {sessions.map((session) => (
                <button
                  type="button"
                  key={session.id}
                  className={cn(
                    'group relative flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-sm cursor-pointer transition-all text-left',
                    activeSession?.id === session.id
                      ? 'bg-white dark:bg-zinc-900 font-medium text-foreground shadow-sm ring-1 ring-border/50'
                      : 'text-muted-foreground hover:bg-black/5 dark:hover:bg-white/5 hover:text-foreground'
                  )}
                  onClick={() => onSelectSession(session.id)}
                >
                  {activeSession?.id === session.id && (
                    <div className="absolute left-0 top-1/2 -translate-y-1/2 h-5 w-1 rounded-r-md bg-primary shrink-0" />
                  )}
                  <span className="truncate flex-1 pl-1">{session.title || 'New Chat'}</span>
                  {activeSession?.id !== session.id && (
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
                <div className="py-12 text-center text-sm text-muted-foreground flex flex-col items-center gap-3">
                  <div className="h-12 w-12 rounded-full bg-muted/50 flex items-center justify-center">
                    <History className="h-5 w-5 opacity-20" />
                  </div>
                  No chat history
                </div>
              )}
            </div>
          </div>
        )}

        {/* Settings Panel */}
        {tab === 'settings' && (
          <div className="h-full overflow-y-auto px-4 pb-6 space-y-4">
            {/* Theme Card */}
            <div className="rounded-xl border border-border/50 bg-background/50 backdrop-blur shadow-sm p-4 space-y-3">
              <div className="flex items-center gap-2 text-xs font-semibold text-foreground tracking-tight">
                <Laptop className="h-3.5 w-3.5 text-muted-foreground" />
                Appearance
              </div>
              <div className="grid grid-cols-3 gap-1.5 bg-muted/50 p-1 rounded-lg">
                <button
                  type="button"
                  onClick={() => setTheme('light')}
                  className={cn(
                    'flex items-center justify-center rounded-md py-1.5 transition-all text-xs font-medium',
                    theme === 'light'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  )}
                >
                  <Sun className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => setTheme('dark')}
                  className={cn(
                    'flex items-center justify-center rounded-md py-1.5 transition-all text-xs font-medium',
                    theme === 'dark'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  )}
                >
                  <Moon className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => setTheme('system')}
                  className={cn(
                    'flex items-center justify-center rounded-md py-1.5 transition-all text-xs font-medium',
                    theme === 'system'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  )}
                >
                  <Laptop className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            {/* Workspace Card */}
            <div className="rounded-xl border border-border/50 bg-background/50 backdrop-blur shadow-sm p-4 space-y-3">
              <div className="flex items-center justify-between text-xs font-semibold text-foreground tracking-tight">
                <div className="flex items-center gap-2">
                  <FolderOpen className="h-3.5 w-3.5 text-muted-foreground" />
                  Workspace
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => setPickerOpen(true)}
                  className="h-6 w-6 shrink-0 text-muted-foreground hover:text-primary transition-colors"
                >
                  <Settings className="h-3.5 w-3.5" />
                </Button>
              </div>
              <div className="min-w-0 rounded-lg bg-muted/40 p-2.5">
                <p
                  className="break-all font-mono text-[11px] leading-relaxed text-muted-foreground"
                  title={config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd}
                >
                  {config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd}
                </p>
              </div>
            </div>

            {/* LLM Provider Card */}
            <div className="rounded-xl border border-border/50 bg-background/50 backdrop-blur shadow-sm p-4 space-y-4">
              <div className="flex items-center gap-2 text-xs font-semibold text-foreground tracking-tight">
                <Code2 className="h-3.5 w-3.5 text-muted-foreground" />
                Provider
              </div>
              <div className="space-y-4">
                {/* Provider selector */}
                {remoteConfig?.providers && Object.keys(remoteConfig.providers).length > 0 && (
                  <div className="space-y-1.5 pt-2 border-t border-border/50">
                    <label
                      htmlFor="provider-select"
                      className="pl-1 text-[11px] font-medium text-muted-foreground uppercase tracking-wider"
                    >
                      Provider
                    </label>
                    <select
                      id="provider-select"
                      value={config.provider || ''}
                      onChange={(e) => handleProviderChange(e.target.value)}
                      className={SELECT_CLASS}
                    >
                      <option value="">— Select Provider —</option>
                      {Object.values(remoteConfig.providers).map((p) => (
                        <option key={p.name} value={p.name}>
                          {p.name} ({p.type})
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                {/* Model selector */}
                {providerModels.length > 0 ? (
                  <div className="space-y-1.5 pt-2 border-t border-border/50">
                    <label
                      htmlFor="model-input"
                      className="pl-1 text-[11px] font-medium text-muted-foreground uppercase tracking-wider"
                    >
                      Model
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
                  <div className="pt-2 text-center text-xs text-muted-foreground">
                    No models available. Please select a valid provider.
                  </div>
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
