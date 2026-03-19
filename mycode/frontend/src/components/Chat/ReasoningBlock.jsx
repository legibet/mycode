/**
 * Reasoning/thinking display.
 * Soft background section — visually grouped, no border.
 * Auto-collapses when streaming ends.
 */

import { ChevronDown } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { cn } from '../../utils/cn'

export function ReasoningBlock({ content, isStreaming }) {
  const [expanded, setExpanded] = useState(isStreaming)
  const wasStreaming = useRef(isStreaming)

  useEffect(() => {
    if (wasStreaming.current && !isStreaming) {
      setExpanded(false)
    }
    wasStreaming.current = isStreaming
  }, [isStreaming])

  if (!content) return null

  return (
    <div className="rounded-lg bg-secondary/30 px-3 py-2">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <span
          className={cn(
            'text-xs transition-colors',
            isStreaming
              ? 'text-accent/70 animate-pulse font-medium'
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
        />
      </button>

      <div
        className={cn(
          'grid transition-all duration-200 ease-out',
          expanded
            ? 'grid-rows-[1fr] opacity-100'
            : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="overflow-hidden">
          <div className="pt-2 text-[13px] text-muted-foreground whitespace-pre-wrap font-mono leading-[1.5]">
            {content}
          </div>
        </div>
      </div>
    </div>
  )
}
