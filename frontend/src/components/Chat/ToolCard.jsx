/**
 * Tool execution display.
 * Soft background section — same visual language as ReasoningBlock.
 * Compact trigger line, expandable body with code-styled content.
 */

import {
  ChevronDown,
  FilePen,
  FilePlus2,
  FileText,
  Loader2,
  Terminal,
} from 'lucide-react'
import { useState } from 'react'
import { cn } from '../../utils/cn'

const TOOL_META = {
  read: { icon: FileText, label: 'read' },
  write: { icon: FilePlus2, label: 'write' },
  edit: { icon: FilePen, label: 'edit' },
  bash: { icon: Terminal, label: 'bash' },
}

/** Extract a concise, human-readable preview for the trigger line. */
function getPreview(name, args) {
  if (!args) return ''
  switch (name) {
    case 'bash':
      return args.command || ''
    case 'read':
    case 'write':
    case 'edit':
      return args.path || ''
    default:
      return Object.entries(args)
        .filter(([k]) => k !== 'content' && k !== 'prompt')
        .map(([, v]) => (typeof v === 'object' ? '…' : String(v)))
        .join(' ')
  }
}

export function ToolCard({ name, args, output, result, pending, isError }) {
  const display =
    typeof result === 'string'
      ? result
      : typeof output === 'string'
        ? output
        : ''
  const resolvedIsError =
    Boolean(isError) ||
    (display && typeof display === 'string' && display.startsWith('error:'))
  const [expandedOverride, setExpandedOverride] = useState(null)
  const expanded = expandedOverride ?? resolvedIsError

  const hasResult = display !== null && display !== undefined && display !== ''
  const status = pending ? 'pending' : resolvedIsError ? 'error' : 'success'

  const meta = TOOL_META[name] || { icon: Terminal, label: name }
  const Icon = meta.icon
  const preview = getPreview(name, args)

  return (
    <div
      className={cn(
        'rounded-lg px-3 py-2 transition-colors',
        status === 'error' ? 'bg-red-500/[0.06]' : 'bg-secondary/30',
      )}
    >
      {/* Trigger */}
      <button
        type="button"
        className="flex w-full items-center gap-2 select-none cursor-pointer text-left"
        onClick={() => setExpandedOverride(!expanded)}
      >
        <Icon
          className={cn(
            'h-3.5 w-3.5 shrink-0',
            status === 'error' ? 'text-red-400/70' : 'text-muted-foreground/40',
          )}
        />

        <span
          className={cn(
            'text-[13px] font-medium shrink-0',
            status === 'error' ? 'text-red-400' : 'text-foreground/80',
          )}
        >
          {name}
        </span>

        {!expanded && preview && (
          <span className="text-[13px] text-muted-foreground/50 font-mono truncate">
            {preview}
          </span>
        )}

        <span className="flex-1" />

        {status === 'pending' && (
          <Loader2 className="h-3.5 w-3.5 text-muted-foreground/40 animate-spin shrink-0" />
        )}

        <ChevronDown
          className={cn(
            'h-3 w-3 text-muted-foreground/30 transition-transform duration-200 shrink-0',
            !expanded && '-rotate-90',
          )}
        />
      </button>

      {/* Body */}
      <div
        className={cn(
          'grid transition-all duration-200 ease-out',
          expanded
            ? 'grid-rows-[1fr] opacity-100'
            : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="overflow-hidden">
          <div className="pt-2 space-y-2">
            {args && Object.keys(args).length > 0 && (
              <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto">
                {Object.entries(args).map(([key, value]) => (
                  <div key={key}>
                    <span className="text-accent/60">{key}: </span>
                    <span className="text-foreground/70 break-all whitespace-pre-wrap">
                      {typeof value === 'object'
                        ? JSON.stringify(value, null, 2)
                        : String(value)}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {hasResult && (
              <div
                className={cn(
                  'rounded-md px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto whitespace-pre-wrap max-h-[240px] overflow-y-auto',
                  status === 'error'
                    ? 'bg-red-500/[0.06] text-red-400/80'
                    : 'bg-code text-muted-foreground',
                )}
              >
                {display}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
