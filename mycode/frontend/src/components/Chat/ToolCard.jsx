/**
 * Collapsible tool execution card.
 * Minimal collapsed state: name + args preview + status dot.
 * Progress bar animation for running state.
 */

import { ArrowRight, ChevronRight, Code2, FileText, Globe, Terminal, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '../../utils/cn'

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

  useEffect(() => {
    if (isError) setExpanded(true)
  }, [isError])

  const Icon = getToolIcon(name)
  const hasResult = result !== null && result !== undefined
  const status = pending ? 'pending' : isError ? 'error' : 'success'

  // Status dot colors
  const dotColor = {
    pending: 'bg-amber-400',
    error: 'bg-red-400',
    success: 'bg-emerald-400',
  }[status]

  // Compact args preview
  const argPreview = args
    ? Object.entries(args)
        .map(([key, value]) => {
          if (key === 'content' || key === 'prompt') return null
          const valStr = typeof value === 'object' ? '...' : String(value)
          return `${key}=${valStr}`
        })
        .filter(Boolean)
        .join(' ')
    : ''

  return (
    <div
      className={cn(
        'my-1.5 rounded-md border overflow-hidden transition-all duration-200',
        status === 'pending' ? 'border-amber-500/20' : status === 'error' ? 'border-red-500/20' : 'border-border/40'
      )}
    >
      {/* Header — always visible */}
      <button
        type="button"
        className={cn(
          'flex w-full cursor-pointer items-center gap-2.5 px-3 py-2 select-none transition-colors text-left',
          expanded ? 'bg-secondary/50' : 'hover:bg-secondary/30'
        )}
        onClick={() => setExpanded(!expanded)}
      >
        <Icon className="h-3.5 w-3.5 text-muted-foreground/60 shrink-0" />

        <span className="font-mono text-xs font-medium text-foreground/80">{name}</span>

        {/* Args preview (collapsed only) */}
        {!expanded && argPreview && (
          <span className="text-2xs text-muted-foreground/40 font-mono truncate flex-1">{argPreview}</span>
        )}

        {!expanded && !argPreview && <span className="flex-1" />}

        {/* Status dot */}
        <div className="flex items-center gap-1.5 shrink-0">
          {status === 'pending' && <span className="text-2xs text-amber-400/70 font-mono">running</span>}
          <div className={cn('h-1.5 w-1.5 rounded-full shrink-0', dotColor, status === 'pending' && 'animate-pulse')} />
        </div>

        <ChevronRight
          className={cn(
            'h-3 w-3 text-muted-foreground/30 transition-transform duration-200 shrink-0',
            expanded ? 'rotate-90' : ''
          )}
        />
      </button>

      {/* Progress bar for running state */}
      {status === 'pending' && (
        <div className="h-[1px] bg-border/30 overflow-hidden">
          <div className="h-full w-1/3 bg-amber-400/50 animate-progress-line" />
        </div>
      )}

      {/* Expandable body */}
      <div
        className={cn('grid transition-all duration-200 ease-in-out', expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}
      >
        <div className="overflow-hidden">
          <div className="px-3 py-2.5 space-y-2.5 border-t border-border/30">
            {/* Input */}
            {args && Object.keys(args).length > 0 && (
              <div className="space-y-1">
                <div className="flex items-center gap-1 text-2xs font-mono text-muted-foreground/50 uppercase tracking-widest">
                  <ArrowRight className="h-2.5 w-2.5" /> input
                </div>
                <div className="bg-code rounded px-2.5 py-2 font-mono text-xs overflow-x-auto">
                  <ArgDisplay args={args} />
                </div>
              </div>
            )}

            {/* Output */}
            {hasResult && (
              <div className="space-y-1">
                <div className="flex items-center gap-1 text-2xs font-mono uppercase tracking-widest">
                  {status === 'error' ? (
                    <XCircle className="h-2.5 w-2.5 text-red-400" />
                  ) : (
                    <Terminal className="h-2.5 w-2.5 text-muted-foreground/50" />
                  )}
                  <span className={status === 'error' ? 'text-red-400/70' : 'text-muted-foreground/50'}>output</span>
                </div>
                <div
                  className={cn(
                    'rounded px-2.5 py-2 font-mono text-xs overflow-x-auto whitespace-pre-wrap max-h-[300px] overflow-y-auto',
                    status === 'error' ? 'bg-red-500/5 text-red-400/80' : 'bg-code text-muted-foreground'
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
    <div className="space-y-0.5">
      {Object.entries(args).map(([key, value]) => (
        <div key={key}>
          <span className="text-accent/60 mr-1.5">{key}:</span>
          <span className="text-foreground/70 break-all whitespace-pre-wrap">
            {typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
          </span>
        </div>
      ))}
    </div>
  )
}
