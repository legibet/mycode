import { Code2, Menu, Settings, Trash2 } from 'lucide-react'
import { Button } from './UI/Button'

export function Header({ config, status, onClear, onOpenSettings }) {
  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur-md">
      <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Code2 className="h-5 w-5" />
          </div>
          <div className="flex flex-col">
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
    </header>
  )
}
