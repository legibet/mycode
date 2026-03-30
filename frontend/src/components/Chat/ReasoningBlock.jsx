/**
 * Reasoning/thinking display.
 * Soft background section — visually grouped, no border.
 * Auto-collapses when streaming ends.
 */

import { ChevronDown } from 'lucide-react'
import { memo, useState } from 'react'
import { cn } from '../../utils/cn'

export const ReasoningBlock = memo(function ReasoningBlock({
  content,
  isStreaming,
}) {
  const [expandedOverride, setExpandedOverride] = useState(null)
  const expanded = expandedOverride ?? isStreaming

  if (!content) return null

  return (
    <div className="rounded-lg bg-secondary/30 px-3 py-2">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpandedOverride(!expanded)}
      >
        <span
          className={cn(
            'text-xs transition-colors',
            isStreaming
              ? 'text-accent/70 animate-breathing font-medium'
              : 'text-muted-foreground/60',
          )}
        >
          Thinking
        </span>
        <ChevronDown
          className={cn(
            'h-3 w-3 text-muted-foreground/30 transition-transform duration-200',
            !expanded && '-rotate-90',
          )}
          aria-hidden="true"
        />
      </button>

      <div
        className={cn(
          'grid transition-[grid-template-rows,opacity] duration-200 ease-out',
          expanded
            ? 'grid-rows-[1fr] opacity-100'
            : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="overflow-hidden">
          <div className="pt-2 text-[13px] text-muted-foreground whitespace-pre-wrap italic leading-[1.5]">
            {content}
          </div>
        </div>
      </div>
    </div>
  )
})
