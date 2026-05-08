/**
 * Tool execution display.
 * Zero container. Two-tier typography: tool name (sans medium, foreground)
 * vs everything else (mono regular, muted-foreground). Click the row text
 * to toggle. Per-tool bodies (bash / read / write / edit / generic) are
 * preserved.
 */

import { FileText, PenLine, SquarePen, Terminal } from 'lucide-react'
import { lazy, memo, Suspense, useState } from 'react'
import { cn } from '../../utils/cn'

const EditDiff = lazy(() => import('./EditDiff'))

interface EditEntry {
  oldText: string
  newText: string
}

// Tool inputs/outputs are JSON from the model; treat fields as `unknown` and
// type-check at the read site instead of trusting the shape with `as`.
type Args = Record<string, unknown> | undefined
type Meta = Record<string, unknown> | null | undefined

interface BashArgs {
  command?: unknown
}
interface PathArgs {
  path?: unknown
}
interface ReadArgs {
  offset?: unknown
  limit?: unknown
}
interface WriteArgs {
  content?: unknown
}
interface EditArgs {
  edits?: unknown
}
interface EditMeta {
  patch?: unknown
  added_lines?: unknown
  removed_lines?: unknown
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function getEdits(args: Args): EditEntry[] | null {
  const edits = (args as EditArgs | undefined)?.edits
  return Array.isArray(edits) ? (edits as EditEntry[]) : null
}

function getEditPatch(metadata: Meta): string | null {
  const patch = (metadata as EditMeta | null | undefined)?.patch
  return typeof patch === 'string' && patch ? patch : null
}

function getEditStats(
  metadata: Meta,
): { added: number; removed: number } | null {
  const meta = metadata as EditMeta | null | undefined
  const added = asNumber(meta?.added_lines)
  const removed = asNumber(meta?.removed_lines)
  if (added == null || removed == null) return null
  return { added, removed }
}

function EditDiffFallback({ edits }: { edits: EditEntry[] }) {
  return (
    <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-normal overflow-x-auto scrollbar-subtle whitespace-pre-wrap">
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
          ? 'bg-red-500/5 text-red-400/70'
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

function getPreview(name: string, args: Args): string {
  if (!args) return ''
  switch (name) {
    case 'bash':
      return asString((args as BashArgs).command)
    case 'read':
    case 'write':
    case 'edit':
      return asString((args as PathArgs).path)
    default: {
      const values: string[] = []
      for (const [key, value] of Object.entries(args)) {
        if (key === 'content' || key === 'prompt') continue
        values.push(typeof value === 'object' ? '…' : String(value))
      }
      return values.join(' ')
    }
  }
}

function getReadHint(args: Args): string {
  const a = args as ReadArgs | undefined
  const offset = asNumber(a?.offset)
  const limit = asNumber(a?.limit)
  if (offset != null && limit != null) return `:${offset}-${offset + limit}`
  if (offset != null) return `:${offset}`
  if (limit != null) return `:1-${limit}`
  return ''
}

function getWriteHint(args: Args): string {
  const content = asString((args as WriteArgs | undefined)?.content)
  if (!content) return ''
  return `${content.split('\n').length} lines`
}

function CollapsedSuffix({
  name,
  args,
  metadata,
}: {
  name: string
  args: Args
  metadata: Meta
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
  args: Args
  display: string
  isError: boolean
}) {
  const command = asString((args as BashArgs | undefined)?.command)

  return (
    <div className="space-y-2">
      {command && (
        <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-normal overflow-x-auto scrollbar-subtle">
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
  args: Args
  display: string
  isError: boolean
}) {
  const content = asString((args as WriteArgs | undefined)?.content)

  return (
    <div className="space-y-2">
      {content && !isError && (
        <div className="rounded-md bg-code px-3 py-2 font-mono text-[13px] leading-normal overflow-x-auto overflow-y-auto scrollbar-subtle whitespace-pre-wrap max-h-60 text-foreground/75">
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
  args: Args
  metadata: Meta
  display: string
  isError: boolean
}) {
  const edits = getEdits(args)
  if (edits?.length) {
    const patch = getEditPatch(metadata)
    return (
      <div className="space-y-2">
        {patch ? (
          <Suspense fallback={<EditDiffFallback edits={edits} />}>
            <EditDiff patch={patch} />
          </Suspense>
        ) : (
          <EditDiffFallback edits={edits} />
        )}
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
