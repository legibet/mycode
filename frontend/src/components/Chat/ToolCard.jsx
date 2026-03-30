/**
 * Tool execution display.
 * Soft background section — same visual language as ReasoningBlock.
 * Compact trigger line, expandable body with code-styled content.
 */

import {
  Check,
  ChevronDown,
  FileText,
  Loader2,
  PenLine,
  SquarePen,
  Terminal,
} from 'lucide-react'
import { lazy, memo, Suspense, useState } from 'react'
import { cn } from '../../utils/cn'

let editDiffPromise
function loadEditDiff() {
  if (!editDiffPromise) editDiffPromise = import('./EditDiff')
  return editDiffPromise
}
const EditDiff = lazy(loadEditDiff)

function EditDiffFallback({ oldText, newText }) {
  return (
    <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto whitespace-pre-wrap">
      {oldText && <div className="diff-line-removed px-1">{oldText}</div>}
      {newText && <div className="diff-line-added px-1">{newText}</div>}
    </div>
  )
}

const TOOL_META = {
  read: { icon: FileText, label: 'read' },
  write: { icon: PenLine, label: 'write' },
  edit: { icon: SquarePen, label: 'edit' },
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

export const ToolCard = memo(function ToolCard({
  name,
  args,
  output,
  modelText,
  displayText,
  pending,
  isError,
}) {
  const display =
    typeof displayText === 'string'
      ? displayText
      : typeof output === 'string'
        ? output
        : ''
  const resolvedIsError =
    Boolean(isError) ||
    (typeof modelText === 'string' && modelText.startsWith('error:'))
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
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        <Icon
          className={cn(
            'h-3.5 w-3.5 shrink-0',
            status === 'error' ? 'text-red-400' : 'text-foreground/70',
          )}
          aria-hidden="true"
        />

        <span
          className={cn(
            'text-[13px] font-medium shrink-0',
            status === 'error' ? 'text-red-400' : 'text-foreground/70',
          )}
        >
          {name}
        </span>

        {!expanded && preview && (
          <span className="pl-1 text-[13px] text-muted-foreground/50 font-mono truncate">
            {preview}
          </span>
        )}

        <span className="flex-1" />

        {status === 'pending' && (
          <Loader2
            className="h-3.5 w-3.5 text-muted-foreground/40 animate-spin shrink-0"
            aria-hidden="true"
          />
        )}
        {status === 'success' && (
          <Check
            className="h-3 w-3 text-emerald-500/50 shrink-0"
            aria-hidden="true"
          />
        )}

        <ChevronDown
          className={cn(
            'h-3 w-3 text-muted-foreground/30 transition-transform duration-200 shrink-0',
            !expanded && '-rotate-90',
          )}
          aria-hidden="true"
        />
      </button>

      {/* Body */}
      <div
        className={cn(
          'grid transition-[grid-template-rows,opacity] duration-200 ease-out',
          expanded
            ? 'grid-rows-[1fr] opacity-100'
            : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="overflow-hidden">
          <div className="pt-2 space-y-2">
            {args &&
              Object.keys(args).length > 0 &&
              (name === 'edit' && args.oldText !== undefined ? (
                <Suspense
                  fallback={
                    <EditDiffFallback
                      oldText={args.oldText}
                      newText={args.newText}
                    />
                  }
                >
                  <EditDiff
                    path={args.path}
                    oldText={args.oldText}
                    newText={args.newText}
                    result={modelText}
                  />
                </Suspense>
              ) : (
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
              ))}

            {hasResult && !(name === 'edit' && !resolvedIsError) && (
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
})
