import { Check, Code2, History, Menu, Plus, Settings, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import { Button } from './UI/Button'

const formatTimestamp = (value) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString()
}

export function Header({
  config,
  status,
  sessions,
  activeSession,
  sessionLoading,
  onClear,
  onCreateSession,
  onSelectSession,
  onDeleteSession,
  onOpenSettings,
}) {
  const [panelOpen, setPanelOpen] = useState(false)

  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Code2 className="h-5 w-5" />
          </div>
          <div className="flex min-w-0 flex-col">
            <h1 className="text-sm font-semibold tracking-tight">mycode</h1>
            <div className="flex items-center gap-2 text-[10px]">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  status === 'generating'
                    ? 'bg-amber-500 animate-pulse'
                    : status === 'ready'
                      ? 'bg-emerald-500'
                      : status === 'offline'
                        ? 'bg-destructive'
                        : 'bg-muted-foreground'
                }`}
              />
              <span className="text-muted-foreground font-medium">{config.model.split(':')[1]}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant={panelOpen ? 'secondary' : 'ghost'}
            size="sm"
            onClick={() => setPanelOpen((prev) => !prev)}
            className="hidden md:flex"
          >
            <History className="mr-2 h-4 w-4" />
            Sessions
          </Button>
          <Button
            variant={panelOpen ? 'secondary' : 'ghost'}
            size="icon"
            onClick={() => setPanelOpen((prev) => !prev)}
            className="md:hidden"
          >
            <History className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClear}
            className="hidden md:flex text-muted-foreground hover:text-foreground"
          >
            <Trash2 className="mr-2 h-4 w-4" />
            Clear
          </Button>
          <Button variant="ghost" size="icon" onClick={onClear} className="md:hidden text-muted-foreground">
            <Trash2 className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onOpenSettings}
            className="hidden md:flex text-muted-foreground hover:text-foreground"
          >
            <Settings className="mr-2 h-4 w-4" />
            Settings
          </Button>
          <Button variant="ghost" size="icon" onClick={onOpenSettings} className="md:hidden text-muted-foreground">
            <Menu className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {panelOpen && (
        <div className="border-t bg-background">
          <div className="mx-auto flex max-w-3xl flex-col gap-3 px-4 py-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <History className="h-4 w-4 text-muted-foreground" />
                <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Sessions</span>
              </div>
              <span className="text-xs text-muted-foreground">{sessions?.length || 0}</span>
            </div>

            <div className="flex items-center gap-2">
              <Button size="sm" onClick={onCreateSession} disabled={sessionLoading} className="flex items-center gap-2">
                <Plus className="h-4 w-4" />
                New Chat
              </Button>
              <span className="text-xs text-muted-foreground">Active: {activeSession?.title || 'New chat'}</span>
            </div>

            <div className="max-h-64 space-y-1 overflow-y-auto rounded-lg border bg-muted/30 p-2">
              {(sessions || []).map((session) => (
                <div
                  key={session.id}
                  className="flex items-center justify-between gap-2 rounded-md px-2 py-2 text-left text-xs hover:bg-muted"
                >
                  <button
                    type="button"
                    onClick={() => onSelectSession(session.id)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div className="truncate font-medium text-foreground">{session.title || 'New chat'}</div>
                    <div className="text-[10px] text-muted-foreground">{formatTimestamp(session.updated_at)}</div>
                  </button>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => onDeleteSession(session.id)}
                    className={`h-6 w-6 ${
                      activeSession?.id === session.id
                        ? 'text-emerald-500 hover:text-emerald-600'
                        : 'text-muted-foreground hover:text-foreground'
                    }`}
                    aria-label={activeSession?.id === session.id ? 'Current session' : 'Delete session'}
                    disabled={activeSession?.id === session.id}
                  >
                    {activeSession?.id === session.id ? (
                      <Check className="h-3.5 w-3.5" />
                    ) : (
                      <X className="h-3.5 w-3.5" />
                    )}
                  </Button>
                </div>
              ))}
              {(sessions || []).length === 0 && (
                <div className="px-2 py-3 text-center text-xs text-muted-foreground">No sessions yet.</div>
              )}
            </div>
          </div>
        </div>
      )}
    </header>
  )
}
