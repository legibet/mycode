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
import type { EditMeta } from '../../types'
import { cn } from '../../utils/cn'

let editDiffPromise: Promise<typeof import('./EditDiff')> | undefined

function loadEditDiff() {
  if (!editDiffPromise) editDiffPromise = import('./EditDiff')
  return editDiffPromise
}
const EditDiff = lazy(loadEditDiff)

interface EditEntry {
  oldText: string
  newText: string
}

interface BashArgs {
  command?: string
  [key: string]: unknown
}

interface PathArgs {
  path?: string
  [key: string]: unknown
}

interface ReadArgs extends PathArgs {
  offset?: number
  limit?: number
}

interface WriteArgs extends PathArgs {
  content?: string
}

interface EditArgs {
  path?: string
  edits?: EditEntry[]
  [key: string]: unknown
}

function isEditArgs(args: Record<string, unknown>): args is EditArgs {
  const editArgs = args as EditArgs
  return (
    (editArgs.path === undefined || typeof editArgs.path === 'string') &&
    Array.isArray(editArgs.edits)
  )
}

/** Parse model_text JSON into per-edit metadata array. */
function parseEditMetas(
  modelText: string | null | undefined,
): EditMeta[] | null {
  if (!modelText) return null
  try {
    const data = JSON.parse(modelText) as {
      status?: string
      edits?: EditMeta[]
    }
    if (data.status === 'ok' && Array.isArray(data.edits)) {
      return data.edits.filter((e) => typeof e?.start_line === 'number')
    }
  } catch {
    /* not JSON */
  }
  return null
}

function EditDiffFallback({ edits }: { edits: EditEntry[] }) {
  return (
    <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto scrollbar-subtle whitespace-pre-wrap">
      {edits.map((entry, i) => (
        <div key={i}>
          {i > 0 && (
            <div className="text-center text-muted-foreground/20 select-none text-xs py-0.5">
              ···
            </div>
          )}
          {entry.oldText && (
            <div className="diff-line-removed px-1">{entry.oldText}</div>
          )}
          {entry.newText && (
            <div className="diff-line-added px-1">{entry.newText}</div>
          )}
        </div>
      ))}
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

// ---------------------------------------------------------------------------
// Shared result block — used by all tool body variants
// ---------------------------------------------------------------------------

const RESULT_BASE =
  'rounded-md px-3 py-2 font-mono text-[13px] leading-relaxed overflow-x-auto overflow-y-auto scrollbar-subtle whitespace-pre-wrap max-h-[240px]'

function ResultBlock({ text, isError }: { text: string; isError: boolean }) {
  if (!text) return null
  return (
    <div
      className={cn(
        RESULT_BASE,
        isError
          ? 'bg-red-500/[0.05] text-red-400/70'
          : 'bg-code text-muted-foreground/80',
      )}
    >
      {text}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helpers for trigger-line previews and collapsed suffixes
// ---------------------------------------------------------------------------

function getPreview(name: string, args?: Record<string, unknown>): string {
  if (!args) return ''
  switch (name) {
    case 'bash': {
      const bashArgs = args as BashArgs
      return typeof bashArgs.command === 'string' ? bashArgs.command : ''
    }
    case 'read':
    case 'write':
    case 'edit': {
      const pathArgs = args as PathArgs
      return typeof pathArgs.path === 'string' ? pathArgs.path : ''
    }
    default:
      return Object.entries(args)
        .filter(([k]) => k !== 'content' && k !== 'prompt')
        .map(([, v]) => (typeof v === 'object' ? '…' : String(v)))
        .join(' ')
  }
}

function getEditStats(
  args?: Record<string, unknown>,
): { added: number; removed: number } | null {
  if (!args) return null
  const editArgs = args as EditArgs
  const edits = editArgs.edits
  if (!Array.isArray(edits)) return null

  let added = 0
  let removed = 0
  for (const entry of edits) {
    if (
      typeof entry?.oldText !== 'string' ||
      typeof entry?.newText !== 'string'
    )
      continue
    const oldSet = new Map<string, number>()
    for (const line of entry.oldText.split('\n'))
      oldSet.set(line, (oldSet.get(line) ?? 0) + 1)
    const newSet = new Map<string, number>()
    for (const line of entry.newText.split('\n'))
      newSet.set(line, (newSet.get(line) ?? 0) + 1)
    for (const [line, count] of oldSet) {
      const nc = newSet.get(line) ?? 0
      if (count > nc) removed += count - nc
    }
    for (const [line, count] of newSet) {
      const oc = oldSet.get(line) ?? 0
      if (count > oc) added += count - oc
    }
  }
  return { added, removed }
}

function getReadHint(args?: Record<string, unknown>): string {
  if (!args) return ''
  const readArgs = args as ReadArgs
  const offset = typeof readArgs.offset === 'number' ? readArgs.offset : null
  const limit = typeof readArgs.limit === 'number' ? readArgs.limit : null
  if (offset != null && limit != null) return `:${offset}-${offset + limit}`
  if (offset != null) return `:${offset}`
  if (limit != null) return `:1-${limit}`
  return ''
}

function getWriteHint(args?: Record<string, unknown>): string {
  if (!args) return ''
  const writeArgs = args as WriteArgs
  const content = writeArgs.content
  if (typeof content !== 'string') return ''
  return `${content.split('\n').length} lines`
}

const SUFFIX_HINT =
  'shrink-0 ml-1.5 text-[12px] font-mono text-muted-foreground/30'

function CollapsedSuffix({
  name,
  args,
}: {
  name: string
  args: Record<string, unknown> | undefined
}) {
  if (name === 'edit') {
    const stats = getEditStats(args)
    if (!stats || (stats.added === 0 && stats.removed === 0)) return null
    return (
      <span className="shrink-0 ml-1.5 text-[12px] font-mono tabular-nums">
        <span className="text-emerald-500/60">+{stats.added}</span>
        <span className="text-red-400/60 ml-1">−{stats.removed}</span>
      </span>
    )
  }

  const hint =
    name === 'read'
      ? getReadHint(args)
      : name === 'write'
        ? getWriteHint(args)
        : ''
  if (!hint) return null
  return <span className={SUFFIX_HINT}>{hint}</span>
}

// ---------------------------------------------------------------------------
// Expanded body components — one per tool type
// ---------------------------------------------------------------------------

function BashBody({
  args,
  display,
  isError,
}: {
  args: Record<string, unknown> | undefined
  display: string
  isError: boolean
}) {
  const bashArgs = args as BashArgs | undefined
  const command = typeof bashArgs?.command === 'string' ? bashArgs.command : ''

  return (
    <div className="pt-2 space-y-2">
      {command && (
        <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto scrollbar-subtle">
          <span className="text-muted-foreground/30 select-none">$ </span>
          <span className="text-foreground/65 whitespace-pre-wrap break-all">
            {command}
          </span>
        </div>
      )}
      <ResultBlock text={display} isError={isError} />
    </div>
  )
}

function ReadBody({
  args,
  display,
  isError,
}: {
  args: Record<string, unknown> | undefined
  display: string
  isError: boolean
}) {
  const readArgs = args as ReadArgs | undefined
  const path = typeof readArgs?.path === 'string' ? readArgs.path : ''
  const hint = getReadHint(args)

  return (
    <div className="pt-2 space-y-2">
      {path && (
        <div className="font-mono text-[13px] text-muted-foreground/40 truncate">
          {path}
          {hint && <span className="text-muted-foreground/30">{hint}</span>}
        </div>
      )}
      <ResultBlock text={display} isError={isError} />
    </div>
  )
}

function WriteBody({
  args,
  display,
  isError,
}: {
  args: Record<string, unknown> | undefined
  display: string
  isError: boolean
}) {
  const writeArgs = args as WriteArgs | undefined
  const path = typeof writeArgs?.path === 'string' ? writeArgs.path : ''
  const content =
    typeof writeArgs?.content === 'string' ? writeArgs.content : ''

  return (
    <div className="pt-2 space-y-2">
      {path && (
        <div className="font-mono text-[13px] text-muted-foreground/40 truncate">
          {path}
        </div>
      )}
      {content && !isError && (
        <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto overflow-y-auto scrollbar-subtle whitespace-pre-wrap max-h-[240px] text-foreground/65">
          {content}
        </div>
      )}
      {isError && <ResultBlock text={display} isError />}
    </div>
  )
}

function EditBody({
  args,
  modelText,
  display,
  isError,
}: {
  args: Record<string, unknown> | undefined
  modelText: string | null | undefined
  display: string
  isError: boolean
}) {
  if (args && isEditArgs(args) && args.edits?.length) {
    const metas = parseEditMetas(modelText)
    const items = args.edits.map((entry, i) => ({
      oldText: entry.oldText,
      newText: entry.newText,
      meta: metas?.[i] ?? null,
    }))
    return (
      <div className="pt-2 space-y-2">
        <Suspense fallback={<EditDiffFallback edits={args.edits} />}>
          <EditDiff path={args.path} edits={items} />
        </Suspense>
        {isError && <ResultBlock text={display} isError />}
      </div>
    )
  }

  return (
    <div className="pt-2 space-y-2">
      {args && Object.keys(args).length > 0 && <GenericArgs args={args} />}
      <ResultBlock text={display} isError={isError} />
    </div>
  )
}

function GenericArgs({ args }: { args: Record<string, unknown> }) {
  return (
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
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

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
      {status === 'pending' && (
        <div className="absolute top-0 left-0 right-0 h-[1px] overflow-hidden rounded-t-lg">
          <div className="h-full w-1/3 bg-accent/30 animate-progress-line" />
        </div>
      )}

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

        {!expanded && <CollapsedSuffix name={name} args={args} />}

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

      <div
        className={cn(
          'grid transition-[grid-template-rows,opacity] duration-250 ease-out',
          expanded
            ? 'grid-rows-[1fr] opacity-100'
            : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="overflow-hidden">
          {name === 'bash' ? (
            <BashBody args={args} display={display} isError={resolvedIsError} />
          ) : name === 'read' ? (
            <ReadBody args={args} display={display} isError={resolvedIsError} />
          ) : name === 'write' ? (
            <WriteBody
              args={args}
              display={display}
              isError={resolvedIsError}
            />
          ) : name === 'edit' ? (
            <EditBody
              args={args}
              modelText={modelText}
              display={display}
              isError={resolvedIsError}
            />
          ) : (
            <div className="pt-2 space-y-2">
              {args && Object.keys(args).length > 0 && (
                <GenericArgs args={args} />
              )}
              <ResultBlock text={display} isError={resolvedIsError} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
})
