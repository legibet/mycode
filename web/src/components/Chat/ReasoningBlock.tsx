/**
 * Reasoning/thinking display.
 * Soft background section — visually grouped, no border.
 * Auto-collapses when streaming ends.
 */

import { ChevronDown } from 'lucide-react'
import { memo, useState } from 'react'
import { cn } from '../../utils/cn'

interface ReasoningBlockProps {
  content: string
  isStreaming?: boolean | undefined
}

export const ReasoningBlock = memo(function ReasoningBlock({
  content,
  isStreaming,
}: ReasoningBlockProps) {
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null)
  const expanded = expandedOverride ?? Boolean(isStreaming)

  if (!content) return null

  return (
    <div className="rounded-lg bg-secondary/20 px-3 py-2">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        <span
          className={cn(
            'text-xs transition-colors duration-200',
            isStreaming
              ? 'text-accent/60 animate-thinking font-medium'
              : 'text-muted-foreground/50',
          )}
        >
          Thinking
        </span>
        <ChevronDown
          className={cn(
            'h-3 w-3 text-muted-foreground/25 transition-transform duration-200',
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
          <div className="pt-2 text-[13px] text-muted-foreground/70 whitespace-pre-wrap italic leading-relaxed">
            {content}
          </div>
        </div>
      </div>
    </div>
  )
})
