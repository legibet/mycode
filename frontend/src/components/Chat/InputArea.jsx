/**
 * Chat input textarea with send/cancel buttons.
 */

import { ArrowUp, Square } from 'lucide-react'
import { useRef } from 'react'
import { Button } from '../UI/Button'

export function InputArea({ input, setInput, loading, onSend, onCancel }) {
  const textareaRef = useRef(null)

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-4">
      <div className="relative rounded-xl border bg-background shadow-sm transition-all focus-within:ring-1 focus-within:ring-ring">
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
          placeholder="Ask for code changes..."
          className="w-full resize-none bg-transparent px-4 py-3 pr-12 text-sm placeholder:text-muted-foreground focus:outline-none scrollbar-hide max-h-[200px]"
          disabled={loading}
          style={{ minHeight: '44px' }}
        />

        <div className="absolute bottom-1.5 right-1.5 flex items-center">
          {loading ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={onCancel}
              className="h-8 w-8 rounded-lg text-muted-foreground hover:text-foreground"
              title="Stop generating"
            >
              <Square className="h-4 w-4 fill-current" />
            </Button>
          ) : (
            <Button
              size="icon"
              variant={input.trim() ? 'primary' : 'ghost'}
              onClick={onSend}
              disabled={!input.trim()}
              className={`h-8 w-8 rounded-lg transition-all ${
                input.trim() ? 'opacity-100' : 'opacity-50 text-muted-foreground hover:bg-transparent'
              }`}
              title="Send message"
            >
              <ArrowUp className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>
      <div className="mt-2 text-center text-[10px] text-muted-foreground">mycode: minimal personal assistant.</div>
    </div>
  )
}
