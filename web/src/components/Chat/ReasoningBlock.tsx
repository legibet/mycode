/**
 * Reasoning / thinking display.
 * Zero container. Label is the click target — 12px Geist Sans regular,
 * breathing during stream. Expanded body uses a 2px left rail (blockquote
 * convention) instead of an indent.
 */

import { memo, useEffect, useRef, useState } from 'react'
import { cn } from '../../utils/cn'

interface ReasoningBlockProps {
  content: string
  isStreaming?: boolean | undefined
}

function formatElapsed(ms: number): string {
  const secs = Math.max(1, Math.round(ms / 1000))
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  const rem = secs % 60
  return rem === 0 ? `${mins}m` : `${mins}m ${rem}s`
}

export const ReasoningBlock = memo(function ReasoningBlock({
  content,
  isStreaming,
}: ReasoningBlockProps) {
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null)
  const [elapsedMs, setElapsedMs] = useState<number | null>(null)
  const startedAtRef = useRef<number | null>(null)

  useEffect(() => {
    if (isStreaming) {
      if (startedAtRef.current == null) startedAtRef.current = Date.now()
      return
    }
    if (startedAtRef.current != null && elapsedMs == null) {
      setElapsedMs(Date.now() - startedAtRef.current)
    }
  }, [isStreaming, elapsedMs])

  if (!content) return null

  const expanded = expandedOverride ?? Boolean(isStreaming)
  const label = isStreaming
    ? 'Thinking…'
    : elapsedMs != null
      ? `Thought · ${formatElapsed(elapsedMs)}`
      : 'Thought'

  return (
    <div className="group/thinking">
      <button
        type="button"
        className="block select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        <span
          className={cn(
            'text-[12px] text-muted-foreground transition-colors duration-200 group-hover/thinking:text-foreground/80',
            isStreaming && 'animate-thinking',
          )}
        >
          {label}
        </span>
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
          <div className="mt-2 border-l-2 border-border pl-3 text-[13px] text-muted-foreground whitespace-pre-wrap leading-relaxed">
            {content}
          </div>
        </div>
      </div>
    </div>
  )
})
