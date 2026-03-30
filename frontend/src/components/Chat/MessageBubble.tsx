/**
 * Message display.
 * No role labels — layout conveys who is speaking.
 * User: right-aligned compact bubble with hover edit button.
 * Assistant: left-aligned, full-width, content-first.
 */

import { Check, Copy, Pencil } from 'lucide-react'
import {
  Component,
  type KeyboardEvent,
  memo,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ChatMessage, MessageBlock } from '../../types'
import { copyText } from '../../utils/clipboard'
import { cn } from '../../utils/cn'
import { MarkdownBlock } from './MarkdownBlock'
import { ReasoningBlock } from './ReasoningBlock'
import { ToolCard } from './ToolCard'

interface MessageBubbleProps {
  role: ChatMessage['role']
  blocks: MessageBlock[]
  sourceIndex?: number | undefined
  synthetic?: boolean | undefined
  isStreaming?: boolean | undefined
  isLoading: boolean
  index: number
  onRewindAndSend?:
    | ((rewindTo: number, input: string) => Promise<void>)
    | undefined
}

interface RenderErrorBoundaryProps {
  children: ReactNode
  fallback: ReactNode
  resetKey: string
}

interface RenderErrorBoundaryState {
  hasError: boolean
  resetKey: string
}

class RenderErrorBoundary extends Component<
  RenderErrorBoundaryProps,
  RenderErrorBoundaryState
> {
  state: RenderErrorBoundaryState = {
    hasError: false,
    resetKey: this.props.resetKey,
  }

  static getDerivedStateFromProps(
    props: RenderErrorBoundaryProps,
    state: RenderErrorBoundaryState,
  ): RenderErrorBoundaryState | null {
    if (props.resetKey === state.resetKey) {
      return null
    }

    return {
      hasError: false,
      resetKey: props.resetKey,
    }
  }

  static getDerivedStateFromError(): Partial<RenderErrorBoundaryState> {
    return { hasError: true }
  }

  componentDidCatch(error: Error) {
    console.error('Chat block render failed:', error)
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback
    }

    return this.props.children
  }
}

export const MessageBubble = memo(function MessageBubble({
  role,
  blocks,
  sourceIndex,
  synthetic,
  isStreaming,
  isLoading,
  index,
  onRewindAndSend,
}: MessageBubbleProps) {
  const isUser = role === 'user'
  const [copied, setCopied] = useState(false)
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState('')
  const editRef = useRef<HTMLTextAreaElement | null>(null)
  const resetCopiedTimeoutRef = useRef<number | null>(null)

  const textContent = useMemo(
    () =>
      blocks
        .filter((block) => block?.type === 'text')
        .map((block) => block.text)
        .join('\n\n'),
    [blocks],
  )

  useEffect(() => {
    return () => {
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current)
      }
    }
  }, [])

  const handleCopy = useCallback(async () => {
    if (!textContent) return
    try {
      await copyText(textContent)
      setCopied(true)
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current)
      }
      resetCopiedTimeoutRef.current = window.setTimeout(() => {
        setCopied(false)
        resetCopiedTimeoutRef.current = null
      }, 2000)
    } catch {
      /* ignore */
    }
  }, [textContent])

  const canEdit =
    isUser &&
    typeof sourceIndex === 'number' &&
    !synthetic &&
    !isLoading &&
    onRewindAndSend

  const startEdit = useCallback(() => {
    setEditText(textContent)
    setEditing(true)
  }, [textContent])

  useEffect(() => {
    if (editing && editRef.current) {
      const el = editRef.current
      el.focus()
      el.style.height = 'auto'
      el.style.height = `${el.scrollHeight}px`
    }
  }, [editing])

  const submitEdit = useCallback(() => {
    const trimmed = editText.trim()
    if (!trimmed || !onRewindAndSend || typeof sourceIndex !== 'number') return
    setEditing(false)
    onRewindAndSend(sourceIndex, trimmed)
  }, [editText, onRewindAndSend, sourceIndex])

  const cancelEdit = useCallback(() => {
    setEditing(false)
  }, [])

  const handleEditKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        submitEdit()
      } else if (e.key === 'Escape') {
        cancelEdit()
      }
    },
    [submitEdit, cancelEdit],
  )

  const renderErrorFallback = (
    <div className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive/80">
      Failed to render this block.
    </div>
  )

  if (isUser) {
    if (editing) {
      return (
        <div
          className="flex justify-end px-5 max-md:px-4 animate-fade-in-up"
          style={{ animationDelay: `${Math.min(index * 30, 150)}ms` }}
        >
          <div className="max-w-[85%] w-full flex flex-col gap-2">
            <textarea
              ref={editRef}
              name="edit-message"
              aria-label="Edit message"
              value={editText}
              onChange={(e) => {
                setEditText(e.target.value)
                e.target.style.height = 'auto'
                e.target.style.height = `${Math.min(e.target.scrollHeight, 300)}px`
              }}
              onKeyDown={handleEditKeyDown}
              className="w-full resize-none rounded-2xl bg-card px-4 py-2.5 text-sm leading-relaxed text-foreground/90 border border-border/50 focus:outline-none focus:border-accent/50 max-h-[300px]"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={cancelEdit}
                className="px-3 py-1 text-xs rounded-lg text-muted-foreground hover:text-foreground transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={submitEdit}
                disabled={!editText.trim()}
                className={cn(
                  'px-3 py-1 text-xs rounded-lg transition-colors',
                  editText.trim()
                    ? 'bg-foreground text-background hover:opacity-90'
                    : 'text-muted-foreground/40',
                )}
              >
                Send
              </button>
            </div>
          </div>
        </div>
      )
    }

    return (
      <div
        className="group/user flex justify-end px-5 max-md:px-4 animate-fade-in-up"
        style={{ animationDelay: `${Math.min(index * 30, 150)}ms` }}
      >
        {canEdit && (
          <button
            type="button"
            aria-label="Edit message"
            onClick={startEdit}
            className="self-end mr-2 mb-0.5 opacity-0 group-hover/user:opacity-100 max-md:opacity-60 transition-opacity duration-150 h-6 w-6 flex items-center justify-center rounded text-muted-foreground/40 hover:text-muted-foreground/70"
            title="Edit & resend"
          >
            <Pencil className="h-3 w-3" />
          </button>
        )}
        <div className="max-w-[85%] rounded-2xl bg-card px-4 py-2.5 text-sm leading-relaxed text-foreground/90 whitespace-pre-wrap">
          {textContent}
        </div>
      </div>
    )
  }

  return (
    <div
      className="group/msg relative px-5 max-md:px-4 animate-fade-in-up"
      style={{ animationDelay: `${Math.min(index * 30, 150)}ms` }}
    >
      <div className="flex flex-col gap-3 text-foreground/90 leading-relaxed text-sm">
        {blocks.map((block) => {
          if (block.type === 'thinking') {
            const renderKey =
              block.renderKey || `thinking:${block.text || 'block'}`
            return (
              <RenderErrorBoundary
                key={renderKey}
                fallback={renderErrorFallback}
                resetKey={`${renderKey}:${block.text}`}
              >
                <ReasoningBlock
                  content={block.text}
                  isStreaming={isStreaming}
                />
              </RenderErrorBoundary>
            )
          }
          if (block.type === 'text') {
            const renderKey = block.renderKey || `text:${block.text || 'block'}`
            return (
              <RenderErrorBoundary
                key={renderKey}
                fallback={renderErrorFallback}
                resetKey={`${renderKey}:${block.text}`}
              >
                <MarkdownBlock content={block.text} isStreaming={isStreaming} />
              </RenderErrorBoundary>
            )
          }
          if (block.type === 'tool_use') {
            const renderKey =
              block.renderKey || block.id || `tool:${block.name || 'tool'}`
            const resetKey = `${renderKey}:${JSON.stringify(block.input)}:${block.runtime?.pending ? '1' : '0'}:${block.runtime?.isError ? '1' : '0'}:${block.runtime?.output ?? ''}:${block.runtime?.modelText ?? ''}:${block.runtime?.displayText ?? ''}`
            return (
              <RenderErrorBoundary
                key={renderKey}
                fallback={renderErrorFallback}
                resetKey={resetKey}
              >
                <ToolCard
                  name={block.name}
                  args={block.input}
                  output={block.runtime?.output}
                  modelText={block.runtime?.modelText}
                  displayText={block.runtime?.displayText}
                  pending={block.runtime?.pending}
                  isError={block.runtime?.isError}
                />
              </RenderErrorBoundary>
            )
          }
          return null
        })}

        {isStreaming && (
          <span className="inline-block w-[2px] h-4 bg-accent animate-cursor-blink ml-0.5 align-middle" />
        )}
      </div>

      {!isUser && textContent && !isStreaming && (
        <div className="mt-2 max-md:opacity-60 opacity-0 group-hover/msg:opacity-100 transition-opacity duration-150">
          <button
            type="button"
            aria-label="Copy to clipboard"
            onClick={handleCopy}
            className={cn(
              'flex items-center justify-center h-6 w-6 rounded transition-colors duration-150',
              copied
                ? 'text-emerald-400'
                : 'text-muted-foreground/40 hover:text-muted-foreground/70',
            )}
            title="Copy"
          >
            {copied ? (
              <Check className="h-3.5 w-3.5" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      )}
    </div>
  )
})
