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
  'w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs font-mono text-foreground shadow-sm outline-none focus:ring-1 focus:ring-ring disabled:opacity-50'

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
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Current path</div>
                  <p className="mt-1 break-all font-mono text-xs text-foreground">{config.cwd}</p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setPickerOpen(true)}
                  className="shrink-0"
                >
                  <FolderOpen className="mr-2 h-4 w-4" />
                  Choose
                </Button>
              </div>
            </div>

            {/* LLM Provider */}
            <div className="space-y-3">
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">LLM Provider</span>
              <div className="space-y-3">
                {/* Provider selector — from config.json */}
                {remoteConfig?.providers && Object.keys(remoteConfig.providers).length > 0 ? (
                  <div className="space-y-1">
                    <label htmlFor="provider-select" className="text-xs text-muted-foreground">
                      Provider
                    </label>
                    <select
                      id="provider-select"
                      value={config.provider || ''}
                      onChange={(e) => handleProviderChange(e.target.value)}
                      className={SELECT_CLASS}
                    >
                      <option value="">— server default —</option>
                      {Object.values(remoteConfig.providers).map((p) => (
                        <option key={p.name} value={p.name}>
                          {p.name} ({p.type})
                        </option>
                      ))}
                    </select>
                    {/* Provider meta: type badge + key status */}
                    {activeProviderInfo && (
                      <div className="flex items-center gap-2 pt-0.5">
                        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
                          {activeProviderInfo.type}
                        </span>
                        {activeProviderInfo.has_api_key && (
                          <span className="text-[10px] text-green-600 dark:text-green-400">key configured</span>
                        )}
                        {activeProviderInfo.base_url && (
                          <span
                            className="truncate text-[10px] text-muted-foreground"
                            title={activeProviderInfo.base_url}
                          >
                            {activeProviderInfo.base_url.replace(/^https?:\/\//, '')}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                ) : null}

                {/* Model selector */}
                <div className="space-y-1">
                  <label htmlFor="model-input" className="text-xs text-muted-foreground">
                    Model
                  </label>
                  {providerModels.length > 0 ? (
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
                  ) : (
                    <>
                      <Input
                        id="model-input"
                        value={config.model || ''}
                        onChange={(e) => onUpdateConfig({ ...config, model: e.target.value })}
                        placeholder="model name"
                        list="model-presets"
                        className="font-mono text-xs"
                      />
                      <datalist id="model-presets">
                        {allModelOptions.map((m) => (
                          <option key={m} value={m} />
                        ))}
                      </datalist>
                    </>
                  )}
                </div>

                {/* API Key override (only shown when no server key or no provider selected) */}
                {!activeProviderInfo?.has_api_key && (
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
                )}

                {/* Base URL override (only shown when no server base_url or no provider selected) */}
                {!activeProviderInfo?.base_url && (
                  <div className="space-y-1">
                    <label htmlFor="api-base-input" className="text-xs text-muted-foreground">
                      Base URL (optional)
                    </label>
                    <Input
                      id="api-base-input"
                      value={config.apiBase || ''}
                      onChange={(e) => onUpdateConfig({ ...config, apiBase: e.target.value })}
                      placeholder="https://api.example.com/v1"
                      className="font-mono text-xs"
                    />
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
