/**
 * Chat input with accent focus line and floating design.
 */

import { ArrowUp, Square } from 'lucide-react'
import { useRef } from 'react'
import { cn } from '../../utils/cn'

export function InputArea({ input, setInput, loading, onSend, onCancel }) {
  const textareaRef = useRef(null)

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  const hasInput = input.trim().length > 0

  return (
    <div className="mx-auto max-w-4xl px-6 py-4">
      <div className="relative group">
        <div
          className={cn(
            'relative rounded-lg border bg-card/50 transition-all duration-300',
            'border-border/40',
            'focus-within:border-accent/30 focus-within:bg-card',
          )}
        >
          <textarea
            ref={textareaRef}
            rows={1}
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              e.target.style.height = 'auto'
              e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`
            }}
            onKeyDown={handleKeyDown}
            placeholder="ask anything..."
            className="w-full resize-none bg-transparent px-4 py-3 pr-12 text-sm font-sans placeholder:text-muted-foreground/40 placeholder:font-mono focus:outline-none max-h-[200px]"
            disabled={loading}
            style={{ minHeight: '44px' }}
          />

          <div className="absolute bottom-1.5 right-1.5 flex items-center">
            {loading ? (
              <button
                type="button"
                onClick={onCancel}
                className="h-7 w-7 flex items-center justify-center rounded-md text-muted-foreground hover:text-foreground hover:bg-secondary transition-all"
                title="Stop generating"
              >
                <Square className="h-3 w-3 fill-current" />
              </button>
            ) : (
              <button
                type="button"
                onClick={onSend}
                disabled={!hasInput}
                className={cn(
                  'h-7 w-7 flex items-center justify-center rounded-md transition-all duration-200',
                  hasInput
                    ? 'bg-accent text-accent-foreground hover:bg-accent/80'
                    : 'text-muted-foreground/30',
                )}
                title="Send message"
              >
                <ArrowUp className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          {/* Accent focus line at bottom */}
          <div className="absolute bottom-0 left-4 right-4 h-[1px] bg-accent/0 group-focus-within:bg-accent/50 transition-all duration-300" />
        </div>
      </div>

      <div className="mt-2 text-center">
        <span className="text-2xs font-mono text-muted-foreground/30 tracking-wider">
          mycode
        </span>
      </div>
    </div>
  )
}
