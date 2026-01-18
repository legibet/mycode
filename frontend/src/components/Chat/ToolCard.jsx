import {
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  Code2,
  FileText,
  Globe,
  Loader2,
  Terminal,
  XCircle,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '../../utils/cn'

// Helper to pick icon based on tool name
const getToolIcon = (name) => {
  switch (name) {
    case 'bash':
      return Terminal
    case 'read':
    case 'write':
    case 'edit':
      return FileText
    case 'web_search':
      return Globe
    default:
      return Code2
  }
}

export function ToolCard({ name, args, result, pending }) {
  const isError = result && typeof result === 'string' && result.startsWith('error:')
  const [expanded, setExpanded] = useState(false)

  // Auto-expand on error
  useEffect(() => {
    if (isError) setExpanded(true)
  }, [isError])

  const Icon = getToolIcon(name)
  const hasResult = result !== null && result !== undefined

  // Status configuration
  const statusConfig = {
    pending: {
      color: 'text-amber-500',
      bgColor: 'bg-amber-500/10',
      borderColor: 'border-amber-500/50',
      icon: Loader2,
      label: 'Running...',
    },
    error: {
      color: 'text-red-500',
      bgColor: 'bg-red-500/10',
      borderColor: 'border-red-500/50',
      icon: XCircle,
      label: 'Failed',
    },
    success: {
      color: 'text-emerald-500',
      bgColor: 'bg-emerald-500/10',
      borderColor: 'border-emerald-500/50',
      icon: CheckCircle2,
      label: 'Done',
    },
  }

  const status = pending ? 'pending' : isError ? 'error' : 'success'
  const config = statusConfig[status]
  const StatusIcon = config.icon

  // Format args for inline preview (collapsed state)
  const argPreview = args
    ? Object.entries(args)
        .map(([key, value]) => {
          if (key === 'content' || key === 'prompt') return null // Skip long fields in preview
          const valStr = typeof value === 'object' ? '...' : String(value)
          return `${key}=${valStr}`
        })
        .filter(Boolean)
        .join(' ')
    : ''
  const resultPreviewRaw = typeof result === 'string' && result.trim() ? result.trim().split('\n').slice(-1)[0] : ''
  const resultPreview = resultPreviewRaw.length > 120 ? `${resultPreviewRaw.slice(0, 117)}...` : resultPreviewRaw

  return (
    <div
      className={cn(
        'group my-2 rounded-lg border bg-card text-card-foreground shadow-sm overflow-hidden transition-all duration-300',
        // Status-based border styling
        status === 'pending'
          ? 'border-amber-500/40 shadow-[0_0_10px_-3px_rgba(245,158,11,0.2)]'
          : status === 'error'
            ? 'border-red-500/40'
            : 'border-border/60 hover:border-border'
      )}
    >
      {/* Header */}
      <button
        type="button"
        className={cn(
          'flex w-full cursor-pointer items-center gap-3 px-3 py-2.5 select-none transition-colors',
          expanded ? 'bg-muted/30' : 'hover:bg-muted/30'
        )}
        onClick={() => setExpanded(!expanded)}
      >
        {/* Icon Box */}
        <div
          className={cn(
            'flex h-8 w-8 shrink-0 items-center justify-center rounded-md border transition-all duration-300',
            config.bgColor,
            config.borderColor,
            config.color
          )}
        >
          <Icon className="h-4 w-4" />
        </div>

        {/* Main Info */}
        <div className="flex flex-1 flex-col min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm tracking-tight">{name}</span>
            {/* Status Badge */}
            <div
              className={cn(
                'flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-medium border',
                config.bgColor,
                config.borderColor,
                config.color,
                status === 'pending' ? 'animate-pulse' : ''
              )}
            >
              <StatusIcon className={cn('h-3 w-3', status === 'pending' ? 'animate-spin' : '')} />
              <span>{config.label}</span>
            </div>
          </div>

          {/* Preview for collapsed state */}
          {!expanded && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground/60 truncate mt-0.5 font-mono">
              {argPreview && <span>{argPreview}</span>}
              {args && (args.content || args.prompt) && <span className="italic opacity-50">...content...</span>}
              {resultPreview && <span className="text-foreground/70">output: {resultPreview}</span>}
            </div>
          )}
        </div>

        {/* Chevron */}
        <ChevronRight
          className={cn(
            'h-4 w-4 text-muted-foreground/40 transition-transform duration-200',
            expanded ? 'rotate-90 text-muted-foreground' : ''
          )}
        />
      </button>

      {/* Body */}
      <div
        className={cn(
          'grid transition-all duration-200 ease-in-out border-t border-transparent',
          expanded ? 'grid-rows-[1fr] border-border/40' : 'grid-rows-[0fr]'
        )}
      >
        <div className="overflow-hidden">
          <div className="px-3 py-3 space-y-3 bg-muted/5">
            {/* Input Section */}
            {args && Object.keys(args).length > 0 && (
              <div className="space-y-1.5">
                <div className="flex items-center gap-1.5 text-[10px] font-bold text-muted-foreground/70 uppercase tracking-wider">
                  <ArrowRight className="h-3 w-3" /> Input
                </div>
                <div className="bg-muted/40 rounded-md border border-border/40 p-2.5 font-mono text-xs overflow-x-auto">
                  <ArgDisplay args={args} />
                </div>
              </div>
            )}

            {/* Output Section */}
            {hasResult && (
              <div className="space-y-1.5">
                <div className="flex items-center gap-1.5 text-[10px] font-bold text-muted-foreground/70 uppercase tracking-wider">
                  {status === 'error' ? <XCircle className="h-3 w-3 text-red-500" /> : <Terminal className="h-3 w-3" />}
                  {status === 'error' ? 'Error Output' : 'Output'}
                </div>
                <div
                  className={cn(
                    'rounded-md border p-2.5 font-mono text-xs overflow-x-auto whitespace-pre-wrap max-h-[300px] overflow-y-auto custom-scrollbar shadow-sm',
                    status === 'error'
                      ? 'bg-red-50/50 border-red-200 text-red-700 dark:bg-red-900/10 dark:border-red-900/30 dark:text-red-400'
                      : 'bg-background text-muted-foreground border-border/60'
                  )}
                >
                  {result}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function ArgDisplay({ args }) {
  if (!args) return null
  return (
    <div className="space-y-1">
      {Object.entries(args).map(([key, value]) => (
        <div key={key} className="group/arg">
          <span className="text-primary/70 font-semibold mr-2">{key}:</span>
          <span className="text-foreground/80 break-all whitespace-pre-wrap">
            {typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
          </span>
        </div>
      ))}
    </div>
  )
}
