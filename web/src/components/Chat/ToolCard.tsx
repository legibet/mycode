/**
 * Tool execution display.
 * Zero container. Two-tier typography: tool name (sans medium, foreground)
 * vs everything else (mono regular, muted-foreground). Click the row text
 * to toggle. Per-tool bodies (bash / read / write / edit / generic) are
 * preserved.
 */

import { FileText, PenLine, SquarePen, Terminal } from 'lucide-react'
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

function getEditMetas(
  metadata: Record<string, unknown> | null | undefined,
): EditMeta[] | null {
  if (!metadata) return null
  const edits = (metadata as { edits?: unknown }).edits
  if (!Array.isArray(edits)) return null
  return (edits as EditMeta[]).filter((e) => typeof e?.start_line === 'number')
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
  finalOutput?: string | null | undefined
  metadata?: Record<string, unknown> | null | undefined
  pending?: boolean | undefined
  isError?: boolean | undefined
}

// ---------------------------------------------------------------------------
// Shared result block — code viewer for tool output (not a card shell)
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
          : 'bg-code text-muted-foreground',
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
  metadata: Record<string, unknown> | null | undefined,
): { added: number; removed: number } | null {
  if (!metadata) return null
  const edits = (metadata as { edits?: unknown }).edits
  if (!Array.isArray(edits)) return null
  let added = 0
  let removed = 0
  for (const entry of edits as Array<{
    added_lines?: unknown
    removed_lines?: unknown
  }>) {
    if (typeof entry?.added_lines === 'number') added += entry.added_lines
    if (typeof entry?.removed_lines === 'number') removed += entry.removed_lines
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

function CollapsedSuffix({
  name,
  args,
  metadata,
}: {
  name: string
  args: Record<string, unknown> | undefined
  metadata: Record<string, unknown> | null | undefined
}) {
  if (name === 'edit') {
    const stats = getEditStats(metadata)
    if (!stats || (stats.added === 0 && stats.removed === 0)) return null
    return (
      <span className="shrink-0 text-[12px] font-mono tabular-nums">
        <span className="text-emerald-500/70">+{stats.added}</span>
        <span className="text-red-400/70 ml-1">−{stats.removed}</span>
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
  return (
    <span className="shrink-0 text-[12px] font-mono text-muted-foreground/60">
      {hint}
    </span>
  )
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
    <div className="space-y-2">
      {command && (
        <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto scrollbar-subtle">
          <span className="text-muted-foreground/40 select-none">$ </span>
          <span className="text-foreground/75 whitespace-pre-wrap break-all">
            {command}
          </span>
        </div>
      )}
      <ResultBlock text={display} isError={isError} />
    </div>
  )
}

function ReadBody({ display, isError }: { display: string; isError: boolean }) {
  return <ResultBlock text={display} isError={isError} />
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
  const content =
    typeof writeArgs?.content === 'string' ? writeArgs.content : ''

  return (
    <div className="space-y-2">
      {content && !isError && (
        <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-[1.5] overflow-x-auto overflow-y-auto scrollbar-subtle whitespace-pre-wrap max-h-[240px] text-foreground/75">
          {content}
        </div>
      )}
      {isError && <ResultBlock text={display} isError />}
    </div>
  )
}

function EditBody({
  args,
  metadata,
  display,
  isError,
}: {
  args: Record<string, unknown> | undefined
  metadata: Record<string, unknown> | null | undefined
  display: string
  isError: boolean
}) {
  if (args && isEditArgs(args) && args.edits?.length) {
    const metas = getEditMetas(metadata)
    const items = args.edits.map((entry, i) => ({
      oldText: entry.oldText,
      newText: entry.newText,
      meta: metas?.[i] ?? null,
    }))
    return (
      <div className="space-y-2">
        <Suspense fallback={<EditDiffFallback edits={args.edits} />}>
          <EditDiff path={args.path} edits={items} />
        </Suspense>
        {isError && <ResultBlock text={display} isError />}
      </div>
    )
  }

  return (
    <div className="space-y-2">
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
          <span className="text-accent/80">{key}: </span>
          <span className="text-foreground/75 break-all whitespace-pre-wrap">
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
  finalOutput,
  metadata,
  pending,
  isError,
}: ToolCardProps) {
  const display =
    typeof finalOutput === 'string'
      ? finalOutput
      : typeof output === 'string'
        ? output
        : ''
  const resolvedIsError =
    Boolean(isError) ||
    (typeof finalOutput === 'string' && finalOutput.startsWith('error:'))
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null)
  const expanded = expandedOverride ?? resolvedIsError

  const status = pending ? 'pending' : resolvedIsError ? 'error' : 'success'

  const meta = Object.hasOwn(TOOL_META, name)
    ? TOOL_META[name as keyof typeof TOOL_META]
    : { icon: Terminal, label: name }
  const Icon = meta.icon
  const preview = getPreview(name, args)

  const body =
    name === 'bash' ? (
      <BashBody args={args} display={display} isError={resolvedIsError} />
    ) : name === 'read' ? (
      <ReadBody display={display} isError={resolvedIsError} />
    ) : name === 'write' ? (
      <WriteBody args={args} display={display} isError={resolvedIsError} />
    ) : name === 'edit' ? (
      <EditBody
        args={args}
        metadata={metadata}
        display={display}
        isError={resolvedIsError}
      />
    ) : (
      <>
        {args && Object.keys(args).length > 0 && <GenericArgs args={args} />}
        <ResultBlock text={display} isError={resolvedIsError} />
      </>
    )

  return (
    <div className="group/tool">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        <Icon
          className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />

        <span
          className={cn(
            'text-[13px] shrink-0 tracking-tight transition-colors duration-200',
            status === 'error'
              ? 'text-destructive/90 group-hover/tool:text-destructive'
              : 'text-foreground/90 group-hover/tool:text-foreground',
            status === 'pending' && 'animate-thinking',
          )}
        >
          {name}
        </span>

        {preview && (
          <span className="min-w-0 text-[13px] font-mono text-muted-foreground/60 truncate">
            {preview}
          </span>
        )}

        <CollapsedSuffix name={name} args={args} metadata={metadata} />
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
          <div className="mt-2 ml-5">{body}</div>
        </div>
      </div>
    </div>
  )
})
