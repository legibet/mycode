import { BrainCircuit, ChevronRight } from 'lucide-react'
import { useEffect, useState, useRef } from 'react'
import { cn } from '../../utils/cn'

export function ReasoningBlock({ content, isStreaming }) {
  // Auto-expand if streaming when created, otherwise collapse by default for history
  const [expanded, setExpanded] = useState(isStreaming)

  // Auto-scroll to bottom of reasoning if streaming and expanded
  const contentRef = useRef(null)

  if (!content) return null

  return (
    <div className="my-1.5 rounded-md border border-border/40 overflow-hidden transition-all duration-200">
      <button
        type="button"
        className={cn(
          'flex w-full cursor-pointer items-center gap-2.5 px-3 py-2 select-none transition-colors text-left',
          expanded ? 'bg-secondary/50' : 'hover:bg-secondary/30'
        )}
        onClick={() => setExpanded(!expanded)}
      >
        <BrainCircuit className={cn("h-3.5 w-3.5 shrink-0", isStreaming ? "text-accent animate-pulse" : "text-muted-foreground/60")} />
        <span className="font-mono text-xs font-medium text-foreground/70">Thinking</span>

        <span className="flex-1" />

        <ChevronRight
          className={cn(
            'h-3 w-3 text-muted-foreground/30 transition-transform duration-200 shrink-0',
            expanded ? 'rotate-90' : ''
          )}
        />
      </button>

      <div
        className={cn('grid transition-all duration-200 ease-in-out', expanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]')}
      >
        <div className="overflow-hidden">
          <div
            ref={contentRef}
            className="px-4 py-3 border-t border-border/30 bg-muted/20 text-xs text-muted-foreground whitespace-pre-wrap font-mono leading-relaxed"
          >
            {content}
          </div>
        </div>
      </div>
    </div>
  )
}
