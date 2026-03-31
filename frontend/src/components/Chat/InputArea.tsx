/**
 * Chat input area.
 * Shadow-elevated container, clean alignment.
 * Send button bottom-right with theme-adaptive colors.
 */

import { ArrowUp, Square } from 'lucide-react'
import { type KeyboardEvent, memo, useEffect, useRef } from 'react'
import type { SetString } from '../../types'
import { cn } from '../../utils/cn'

interface InputAreaProps {
  input: string
  setInput: SetString
  loading: boolean
  onSend: () => void
  onCancel: () => void
}

export const InputArea = memo(function InputArea({
  input,
  setInput,
  loading,
  onSend,
  onCancel,
}: InputAreaProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (!input && textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [input])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!loading) onSend()
    }
  }

  const hasInput = input.trim().length > 0

  return (
    <div className="mx-auto max-w-4xl max-md:max-w-none px-5 max-md:px-3 py-3 max-md:py-2">
      <div
        className={cn(
          'relative rounded-xl bg-card border border-border/30 shadow-sm transition duration-200',
          'focus-within:shadow-md focus-within:border-border/60',
        )}
      >
        <textarea
          ref={textareaRef}
          rows={1}
          name="message"
          aria-label="Message"
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            e.target.style.height = 'auto'
            e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`
          }}
          onKeyDown={handleKeyDown}
          placeholder="Message…"
          className="block w-full resize-none bg-transparent px-4 py-3 max-md:py-2.5 pr-14 text-base md:text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/40 focus-visible:outline-none max-h-[200px]"
        />

        <div className="absolute bottom-[7px] max-md:bottom-[5px] right-2.5 max-md:right-2">
          {loading ? (
            <button
              type="button"
              aria-label="Stop generating"
              onClick={onCancel}
              className="h-8 w-8 flex items-center justify-center rounded-lg text-destructive/70 hover:text-destructive hover:bg-destructive/10 active:scale-95 transition"
              title="Stop"
            >
              <Square className="h-3.5 w-3.5 fill-current" />
            </button>
          ) : (
            <button
              type="button"
              aria-label="Send message"
              onClick={onSend}
              disabled={!hasInput}
              className={cn(
                'h-8 w-8 flex items-center justify-center rounded-lg transition duration-150',
                hasInput
                  ? 'bg-foreground text-background hover:opacity-90 active:scale-95'
                  : 'text-muted-foreground/40',
              )}
              title="Send"
            >
              <ArrowUp className="h-4 w-4" strokeWidth={2.5} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
})
