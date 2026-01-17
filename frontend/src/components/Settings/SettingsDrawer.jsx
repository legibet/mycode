import { Folder, Laptop, Moon, Sun, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '../../utils/cn'
import { addHistory, loadHistory, MODEL_PRESETS, saveHistory } from '../../utils/storage'
import { useTheme } from '../ThemeProvider'
import { Button } from '../UI/Button'
import { Input } from '../UI/Input'

export function SettingsDrawer({ open, onClose, config, onSave }) {
  const [local, setLocal] = useState(config)
  const [tab, setTab] = useState('api')
  const [history, setHistory] = useState(loadHistory)
  const { theme, setTheme } = useTheme()

  useEffect(() => {
    setLocal(config)
  }, [config])

  const save = () => {
    const nextHistory = addHistory(history, local.cwd)
    setHistory(nextHistory)
    saveHistory(nextHistory)
    onSave(local)
  }

  return (
    <div
      className={cn(
        'fixed inset-0 z-50 transition-all duration-300',
        open ? 'visible' : 'invisible pointer-events-none'
      )}
    >
      <button
        type="button"
        aria-label="Close settings"
        className={cn(
          'absolute inset-0 bg-background/80 backdrop-blur-sm transition-opacity duration-300',
          open ? 'opacity-100' : 'opacity-0'
        )}
        onClick={onClose}
      />
      <div
        className={cn(
          'absolute right-0 top-0 h-full w-full max-w-sm border-l bg-background shadow-2xl transition-transform duration-300',
          open ? 'translate-x-0' : 'translate-x-full'
        )}
      >
        <div className="flex h-full flex-col">
          <div className="flex items-center justify-between border-b px-6 py-4">
            <h2 className="text-lg font-semibold tracking-tight">Settings</h2>
            <Button variant="ghost" size="icon" onClick={onClose} className="h-8 w-8">
              <X className="h-4 w-4" />
            </Button>
          </div>

          <div className="flex items-center gap-2 px-6 pt-4">
            <Button
              variant={tab === 'api' ? 'secondary' : 'ghost'}
              size="sm"
              onClick={() => setTab('api')}
              className="flex-1"
            >
              API
            </Button>
            <Button
              variant={tab === 'workspace' ? 'secondary' : 'ghost'}
              size="sm"
              onClick={() => setTab('workspace')}
              className="flex-1"
            >
              Workspace
            </Button>
            <Button
              variant={tab === 'appearance' ? 'secondary' : 'ghost'}
              size="sm"
              onClick={() => setTab('appearance')}
              className="flex-1"
            >
              Appearance
            </Button>
          </div>

          <div className="flex-1 space-y-6 overflow-y-auto px-6 py-6">
            {tab === 'api' && (
              <div className="space-y-6">
                <div className="space-y-2">
                  <label htmlFor="model-input" className="text-xs font-medium text-muted-foreground">
                    Model
                  </label>
                  <Input
                    id="model-input"
                    value={local.model}
                    onChange={(e) => setLocal({ ...local, model: e.target.value })}
                    placeholder="provider:model"
                    list="model-presets"
                  />
                  <datalist id="model-presets">
                    {MODEL_PRESETS.map((model) => (
                      <option key={model} value={model} />
                    ))}
                  </datalist>
                  <div className="flex flex-wrap gap-2 pt-1">
                    {MODEL_PRESETS.slice(0, 4).map((model) => (
                      <button
                        type="button"
                        key={model}
                        onClick={() => setLocal({ ...local, model })}
                        className="rounded-full border bg-muted/50 px-2.5 py-0.5 text-[10px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
                      >
                        {model.split(':')[1]}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="space-y-2">
                  <label htmlFor="api-key-input" className="text-xs font-medium text-muted-foreground">
                    API Key
                  </label>
                  <Input
                    id="api-key-input"
                    type="password"
                    value={local.apiKey || ''}
                    onChange={(e) => setLocal({ ...local, apiKey: e.target.value })}
                    placeholder="sk-..."
                  />
                  <p className="text-[10px] text-muted-foreground">Stored in session only.</p>
                </div>

                <div className="space-y-2">
                  <label htmlFor="api-base-input" className="text-xs font-medium text-muted-foreground">
                    API Base URL
                  </label>
                  <Input
                    id="api-base-input"
                    value={local.apiBase || ''}
                    onChange={(e) => setLocal({ ...local, apiBase: e.target.value })}
                    placeholder="Optional"
                  />
                </div>
              </div>
            )}

            {tab === 'workspace' && (
              <div className="space-y-6">
                <div className="space-y-2">
                  <label htmlFor="cwd-input" className="text-xs font-medium text-muted-foreground">
                    Working Directory
                  </label>
                  <Input
                    id="cwd-input"
                    value={local.cwd}
                    onChange={(e) => setLocal({ ...local, cwd: e.target.value })}
                    placeholder="/path/to/project"
                  />
                </div>

                <div className="space-y-2">
                  <div className="text-xs font-medium text-muted-foreground">Recent</div>
                  <div className="flex flex-col gap-1">
                    {history.length === 0 && <span className="text-xs text-muted-foreground">No history yet.</span>}
                    {history.map((item) => (
                      <button
                        type="button"
                        key={item}
                        onClick={() => setLocal({ ...local, cwd: item })}
                        className="flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
                      >
                        <Folder className="h-3 w-3" />
                        <span className="truncate">{item}</span>
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {tab === 'appearance' && (
              <div className="space-y-4">
                <div className="space-y-2">
                  <span className="text-xs font-medium text-muted-foreground">Theme</span>
                  <div className="grid grid-cols-3 gap-2">
                    <Button
                      variant={theme === 'light' ? 'primary' : 'outline'}
                      size="sm"
                      onClick={() => setTheme('light')}
                      className="w-full justify-start px-3"
                    >
                      <Sun className="mr-2 h-4 w-4" /> Light
                    </Button>
                    <Button
                      variant={theme === 'dark' ? 'primary' : 'outline'}
                      size="sm"
                      onClick={() => setTheme('dark')}
                      className="w-full justify-start px-3"
                    >
                      <Moon className="mr-2 h-4 w-4" /> Dark
                    </Button>
                    <Button
                      variant={theme === 'system' ? 'primary' : 'outline'}
                      size="sm"
                      onClick={() => setTheme('system')}
                      className="w-full justify-start px-3"
                    >
                      <Laptop className="mr-2 h-4 w-4" /> System
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="border-t bg-muted/20 px-6 py-4">
            <div className="flex gap-3">
              <Button variant="outline" onClick={() => setLocal(config)} className="flex-1">
                Reset
              </Button>
              <Button onClick={save} className="flex-1">
                Save Changes
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
