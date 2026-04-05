/**
 * Tool execution display.
 * Soft background section — same visual language as ReasoningBlock.
 * Compact trigger line, expandable body with code-styled content.
 */

import {
  Check,
  ChevronDown,
  FileText,
  PenLine,
  SquarePen,
  Terminal,
} from 'lucide-react'
import { lazy, memo, Suspense, useState } from 'react'
import { cn } from '../../utils/cn'

let editDiffPromise: Promise<typeof import('./EditDiff')> | undefined

function loadEditDiff() {
  if (!editDiffPromise) editDiffPromise = import('./EditDiff')
  return editDiffPromise
}
const EditDiff = lazy(loadEditDiff)

interface EditArgs {
  path?: string
  oldText?: string
  newText?: string
  [key: string]: unknown
}

/** Validates that edit-tool args have the expected string-or-undefined fields. */
function isEditArgs(args: Record<string, unknown>): args is EditArgs {
  const { oldText, newText, path } = args
  return (
    (oldText === undefined || typeof oldText === 'string') &&
    (newText === undefined || typeof newText === 'string') &&
    (path === undefined || typeof path === 'string')
  )
}

function EditDiffFallback({
  oldText,
  newText,
}: {
  oldText?: string | undefined
  newText?: string | undefined
}) {
  return (
    <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto scrollbar-subtle whitespace-pre-wrap">
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

interface ToolCardProps {
  name: string
  args?: Record<string, unknown>
  output?: string | null | undefined
  modelText?: string | null | undefined
  displayText?: string | null | undefined
  pending?: boolean | undefined
  isError?: boolean | undefined
}

/** Extract a concise, human-readable preview for the trigger line. */
function getPreview(name: string, args?: Record<string, unknown>): string {
  if (!args) return ''
  switch (name) {
    case 'bash':
      // biome-ignore lint/complexity/useLiteralKeys: index signature requires bracket access
      return typeof args['command'] === 'string' ? args['command'] : ''
    case 'read':
    case 'write':
    case 'edit':
      // biome-ignore lint/complexity/useLiteralKeys: index signature requires bracket access
      return typeof args['path'] === 'string' ? args['path'] : ''
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
}: ToolCardProps) {
  const display =
    typeof displayText === 'string'
      ? displayText
      : typeof output === 'string'
        ? output
        : ''
  const resolvedIsError =
    Boolean(isError) ||
    (typeof modelText === 'string' && modelText.startsWith('error:'))
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null)
  const expanded = expandedOverride ?? resolvedIsError

  const hasResult = display !== null && display !== undefined && display !== ''
  const status = pending ? 'pending' : resolvedIsError ? 'error' : 'success'

  const meta = Object.hasOwn(TOOL_META, name)
    ? TOOL_META[name as keyof typeof TOOL_META]
    : { icon: Terminal, label: name }
  const Icon = meta.icon
  const preview = getPreview(name, args)

  return (
    <div
      className={cn(
        'relative rounded-lg px-3 py-2',
        status === 'error' ? 'bg-red-500/[0.05]' : 'bg-secondary/20',
      )}
    >
      {/* Pending progress line */}
      {status === 'pending' && (
        <div className="absolute top-0 left-0 right-0 h-[1px] overflow-hidden rounded-t-lg">
          <div className="h-full w-1/3 bg-accent/30 animate-progress-line" />
        </div>
      )}

      {/* Trigger */}
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        <Icon
          className={cn(
            'h-3.5 w-3.5 shrink-0 transition-colors duration-200',
            status === 'error'
              ? 'text-red-400/80'
              : status === 'pending'
                ? 'text-accent/50'
                : 'text-foreground/60',
          )}
          aria-hidden="true"
        />

        <span
          className={cn(
            'text-[13px] font-medium shrink-0 transition-colors duration-200',
            status === 'error'
              ? 'text-red-400/80'
              : status === 'pending'
                ? 'text-foreground/70'
                : 'text-foreground/60',
          )}
        >
          {name}
        </span>

        {!expanded && preview && (
          <span className="pl-1 text-[13px] text-muted-foreground/40 font-mono truncate">
            {preview}
          </span>
        )}

        <span className="flex-1" />

        {status === 'success' && (
          <Check
            className="h-3 w-3 text-emerald-500/40 shrink-0"
            aria-hidden="true"
          />
        )}

        <ChevronDown
          className={cn(
            'h-3 w-3 text-muted-foreground/25 transition-transform duration-200 shrink-0',
            !expanded && '-rotate-90',
          )}
          aria-hidden="true"
        />
      </button>

      {/* Body */}
      <div
        className={cn(
          'grid transition-[grid-template-rows,opacity] duration-250 ease-out',
          expanded
            ? 'grid-rows-[1fr] opacity-100'
            : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="overflow-hidden">
          <div className="pt-2 space-y-2">
            {args &&
              Object.keys(args).length > 0 &&
              (name === 'edit' &&
              isEditArgs(args) &&
              args.oldText !== undefined ? (
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
                    result={modelText ?? null}
                  />
                </Suspense>
              ) : (
                <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-relaxed overflow-x-auto scrollbar-subtle">
                  {Object.entries(args).map(([key, value]) => (
                    <div key={key}>
                      <span className="text-accent/50">{key}: </span>
                      <span className="text-foreground/65 break-all whitespace-pre-wrap">
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
                  'rounded-md px-3 py-2 font-mono text-[13px] leading-relaxed overflow-x-auto overflow-y-auto scrollbar-subtle whitespace-pre-wrap max-h-[240px]',
                  status === 'error'
                    ? 'bg-red-500/[0.05] text-red-400/70'
                    : 'bg-code text-muted-foreground/80',
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
