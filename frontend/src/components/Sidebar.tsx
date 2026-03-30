/**
 * Sidebar with chat history and settings.
 * Flat, utilitarian design — no card blocks.
 */

import {
  FolderOpen,
  History,
  Laptop,
  Moon,
  Plus,
  Settings,
  Sun,
  Terminal,
  Trash2,
} from 'lucide-react'
import { memo, useState } from 'react'
import type {
  LocalConfig,
  ProviderInfo,
  RemoteConfig,
  SessionSummary,
  Theme,
} from '../types'
import { cn } from '../utils/cn'
import { getDefaultReasoningEffort, isReasoningEffort } from '../utils/config'
import { Button } from './UI/Button'
import { WorkspacePicker } from './WorkspacePicker'

/** Shared select styling */
const SELECT_CLASS =
  'w-full bg-secondary/20 px-2.5 py-2 text-sm font-mono text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-md border-0 focus:bg-secondary/40 disabled:opacity-50 transition-colors cursor-pointer'

const THEME_OPTIONS: Array<{
  key: Theme
  icon: typeof Sun
  label: string
}> = [
  { key: 'light', icon: Sun, label: 'Light' },
  { key: 'dark', icon: Moon, label: 'Dark' },
  { key: 'system', icon: Laptop, label: 'Auto' },
]

function getProviderEntries(
  providers: RemoteConfig['providers'],
): Array<[string, ProviderInfo]> {
  if (!providers) return []

  return Object.keys(providers)
    .map((providerName) => {
      const provider = providers[providerName]
      return provider ? ([providerName, provider] as const) : null
    })
    .filter((entry): entry is [string, ProviderInfo] => entry !== null)
}

interface SidebarProps {
  className?: string
  sessions: SessionSummary[]
  activeSession: SessionSummary | null
  onSelectSession: (id: string) => void
  onCreateSession: () => void
  onDeleteSession: (id: string) => Promise<void>
  config: LocalConfig
  onUpdateConfig: (config: LocalConfig) => void
  cwdHistory: string[]
  remoteConfig: RemoteConfig | null
  theme: Theme
  setTheme: (theme: Theme) => void
}

export const Sidebar = memo(function Sidebar({
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
}: SidebarProps) {
  const [tab, setTab] = useState<'chat' | 'settings'>('chat')
  const [pickerOpen, setPickerOpen] = useState(false)

  const handleWorkspaceSelect = (cwd: string) => {
    onUpdateConfig({ ...config, cwd })
  }

  const handleProviderChange = (providerName: string) => {
    const providerInfo = remoteConfig?.providers?.[providerName]
    const firstModel = providerInfo?.models?.[0] || ''
    onUpdateConfig({
      ...config,
      provider: providerName,
      model: firstModel,
      apiBase: '',
      apiKey: '',
      reasoningEffort: '',
    })
  }

  const providerEntries = getProviderEntries(remoteConfig?.providers)
  const activeProviderInfo = remoteConfig?.providers?.[config.provider]
  const providerModels = activeProviderInfo?.models || []
  const reasoningModels = activeProviderInfo?.reasoning_models || []
  const supportsEffort =
    activeProviderInfo?.supports_reasoning_effort &&
    reasoningModels.includes(config.model)
  const effortOptions = remoteConfig?.reasoning_effort_options || []

  return (
    <div
      className={cn(
        'flex w-64 flex-col border-r border-border/60 bg-sidebar-bg',
        className,
      )}
    >
      {/* Header */}
      <div className="flex h-14 shrink-0 items-center px-4 border-b border-border/40">
        <div className="flex items-center gap-2.5">
          <Terminal className="h-4 w-4 text-accent" />
          <span className="font-display text-sm tracking-tight text-foreground">
            mycode
          </span>
        </div>
      </div>

      {/* Tab navigation — sliding underline */}
      <div
        className="relative flex shrink-0 border-b border-border/40"
        role="tablist"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'chat'}
          onClick={() => setTab('chat')}
          className={cn(
            'flex-1 flex items-center justify-center gap-1.5 py-2.5 text-xs font-medium transition-colors',
            tab === 'chat'
              ? 'text-foreground'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          <History className="h-3 w-3" />
          History
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'settings'}
          onClick={() => setTab('settings')}
          className={cn(
            'flex-1 flex items-center justify-center gap-1.5 py-2.5 text-xs font-medium transition-colors',
            tab === 'settings'
              ? 'text-foreground'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          <Settings className="h-3 w-3" />
          Settings
        </button>
        {/* Sliding indicator */}
        <div
          className="absolute bottom-0 h-[2px] bg-accent transition-[left,transform] duration-200 ease-out"
          style={{
            left: tab === 'chat' ? '12px' : '50%',
            width: 'calc(50% - 24px)',
            transform: tab === 'settings' ? 'translateX(12px)' : 'none',
          }}
        />
      </div>

      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        {/* Chat Sessions List */}
        {tab === 'chat' && (
          <div className="flex h-full flex-col">
            <div className="px-3 pt-2 pb-1 shrink-0">
              <button
                type="button"
                onClick={onCreateSession}
                className="w-full flex items-center justify-center gap-1.5 py-2.5 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/30 rounded-md transition-colors"
              >
                <Plus className="h-3 w-3" />
                New Chat
              </button>
            </div>
            <div className="flex-1 overflow-y-auto pb-4">
              {sessions.map((session) => {
                const isActive = activeSession?.id === session.id
                const isRunning = session.is_running
                return (
                  <div key={session.id} className="group relative">
                    {(isActive || isRunning) && (
                      <div
                        className={cn(
                          'absolute left-0 top-0 bottom-0 w-[2px] bg-accent',
                          isRunning && 'animate-breathing',
                        )}
                      />
                    )}
                    <button
                      type="button"
                      aria-current={isActive ? 'true' : undefined}
                      className={cn(
                        'flex w-full items-center gap-2 px-4 py-2 pr-10 text-xs cursor-pointer transition-colors text-left',
                        isActive
                          ? 'bg-secondary/40 text-foreground'
                          : 'text-muted-foreground hover:text-foreground hover:bg-secondary/20',
                      )}
                      onClick={() => onSelectSession(session.id)}
                    >
                      <span className="truncate flex-1">
                        {session.title || 'New Chat'}
                      </span>
                    </button>
                    {!isActive && (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="absolute right-2 top-1/2 h-5 w-5 -translate-y-1/2 opacity-0 group-hover:opacity-100 transition-opacity"
                        aria-label="Delete session"
                        onClick={(e) => {
                          e.stopPropagation()
                          onDeleteSession(session.id)
                        }}
                      >
                        <Trash2 className="h-3 w-3 text-muted-foreground hover:text-destructive" />
                      </Button>
                    )}
                  </div>
                )
              })}
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
          <div className="h-full overflow-y-auto px-4 py-5 space-y-6">
            {/* Theme */}
            <div>
              <div className="text-2xs font-mono text-muted-foreground/60 mb-2.5">
                Appearance
              </div>
              <div className="flex items-center gap-1">
                {THEME_OPTIONS.map(({ key, icon: Icon, label }) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setTheme(key)}
                    aria-label={label}
                    title={label}
                    className={cn(
                      'flex items-center justify-center h-9 w-9 rounded-md transition-colors',
                      theme === key
                        ? 'text-accent bg-secondary/40'
                        : 'text-muted-foreground hover:text-foreground hover:bg-secondary/20',
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                  </button>
                ))}
              </div>
            </div>

            {/* Workspace */}
            <div>
              <div className="flex items-center justify-between mb-2.5">
                <span className="text-2xs font-mono text-muted-foreground/60">
                  Workspace
                </span>
                <button
                  type="button"
                  aria-label="Open workspace"
                  onClick={() => setPickerOpen(true)}
                  className="text-muted-foreground/50 hover:text-accent transition-colors"
                >
                  <FolderOpen className="h-3 w-3" />
                </button>
              </div>
              <p
                className="break-all font-mono text-2xs leading-relaxed text-muted-foreground"
                title={
                  config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd
                }
              >
                {config.cwd === '.' ? remoteConfig?.cwd || '.' : config.cwd}
              </p>
            </div>

            {/* Provider */}
            <div className="space-y-4">
              {remoteConfig?.providers &&
                Object.keys(remoteConfig.providers).length > 0 && (
                  <div>
                    <label
                      htmlFor="provider-select"
                      className="text-2xs font-mono text-muted-foreground/60"
                    >
                      Provider
                    </label>
                    <select
                      id="provider-select"
                      value={config.provider}
                      onChange={(e) => handleProviderChange(e.target.value)}
                      className={cn(SELECT_CLASS, 'mt-1.5')}
                    >
                      {providerEntries.map(([providerName, p]) => (
                        <option key={providerName} value={providerName}>
                          {p.name} ({p.type})
                        </option>
                      ))}
                    </select>
                  </div>
                )}

              {providerModels.length > 0 ? (
                <div>
                  <label
                    htmlFor="model-input"
                    className="text-2xs font-mono text-muted-foreground/60"
                  >
                    Model
                  </label>
                  <select
                    id="model-input"
                    value={config.model}
                    onChange={(e) => {
                      const nextModel = e.target.value
                      return onUpdateConfig({
                        ...config,
                        model: nextModel,
                        reasoningEffort: '',
                      })
                    }}
                    className={cn(SELECT_CLASS, 'mt-1.5')}
                  >
                    {providerModels.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div className="text-center text-2xs text-muted-foreground/50 py-2">
                  No models available
                </div>
              )}

              {supportsEffort && (
                <div>
                  <label
                    htmlFor="effort-select"
                    className="text-2xs font-mono text-muted-foreground/60"
                  >
                    Reasoning effort
                  </label>
                  <select
                    id="effort-select"
                    value={
                      config.reasoningEffort ||
                      getDefaultReasoningEffort(
                        remoteConfig,
                        config.provider,
                        config.model,
                      ) ||
                      'auto'
                    }
                    onChange={(e) => {
                      const value = e.target.value
                      if (!isReasoningEffort(value)) return
                      onUpdateConfig({
                        ...config,
                        reasoningEffort: value,
                      })
                    }}
                    className={cn(SELECT_CLASS, 'mt-1.5')}
                  >
                    {effortOptions.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>
                </div>
              )}
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
})
