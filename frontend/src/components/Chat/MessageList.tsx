/**
 * Scrollable message list with auto-scroll.
 * Only auto-scrolls when the user is already near the bottom.
 * Empty state: blinking cursor terminal prompt.
 */

import { memo, useCallback, useLayoutEffect, useRef } from 'react'
import type { ChatMessage } from '../../types'
import { MessageBubble } from './MessageBubble'

const SCROLL_THRESHOLD = 120

interface MessageListProps {
  messages: ChatMessage[]
  loading: boolean
  sessionLoading: boolean
  onRewindAndSend: (rewindTo: number, input: string) => Promise<void>
}

export const MessageList = memo(function MessageList({
  messages,
  loading,
  sessionLoading,
  onRewindAndSend,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const endRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)
  const previousMessageCount = useRef(0)

  const handleScroll = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    stickToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD
  }, [])

  useLayoutEffect(() => {
    const previousCount = previousMessageCount.current
    previousMessageCount.current = messages.length

    if (!messages.length || !stickToBottom.current) return

    endRef.current?.scrollIntoView({
      behavior: loading || previousCount === 0 ? 'auto' : 'smooth',
      block: 'end',
    })
  }, [loading, messages])

  if (messages.length === 0) {
    if (sessionLoading) {
      return (
        <div className="flex flex-1 flex-col items-center justify-center p-8">
          <div className="w-full max-w-xl space-y-3">
            <div className="h-3 w-28 animate-pulse rounded bg-secondary/40" />
            <div className="h-4 w-full animate-pulse rounded bg-secondary/25" />
            <div className="h-4 w-5/6 animate-pulse rounded bg-secondary/25" />
            <div className="pt-2 font-mono text-xs text-muted-foreground/70">
              Loading conversation...
            </div>
          </div>
        </div>
      )
    }

    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8">
        <div className="text-center">
          <h1 className="font-display text-2xl tracking-tighter text-foreground/70">
            mycode
            <span className="inline-block w-[2px] h-5 bg-accent/60 ml-0.5 align-middle animate-cursor-blink" />
          </h1>
        </div>
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 overflow-y-auto pb-4 pt-6"
    >
      <div className="mx-auto max-w-4xl max-md:max-w-none flex flex-col gap-6 max-md:gap-5">
        {messages.map((message, index) => (
          <MessageBubble
            key={message.renderKey || `msg-${index}`}
            role={message.role}
            blocks={message.content}
            sourceIndex={message.sourceIndex}
            synthetic={message.meta?.synthetic}
            isStreaming={
              loading &&
              index === messages.length - 1 &&
              message.role === 'assistant'
            }
            isLoading={loading}
            index={index}
            onRewindAndSend={onRewindAndSend}
          />
        ))}
        <div ref={endRef} className="h-4" />
      </div>
    </div>
  )
})
