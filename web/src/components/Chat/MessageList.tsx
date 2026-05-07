/**
 * Scrollable message list with auto-scroll.
 * Only auto-scrolls when the user is already near the bottom.
 * Empty state: blinking cursor terminal prompt.
 */

import { memo, useCallback, useLayoutEffect, useRef } from 'react'
import type { RenderMessage } from '../../types'
import { isCompactMarker } from '../../types'
import { CompactMarker } from './CompactMarker'
import { MessageBubble } from './MessageBubble'

const SCROLL_THRESHOLD = 120
const DRAFT_SESSION_KEY = '__draft__'

interface MessageListProps {
  sessionId?: string | undefined
  messages: RenderMessage[]
  loading: boolean
  sessionLoading: boolean
  onRewindAndSend?:
    | ((rewindTo: number, input: string) => Promise<void>)
    | undefined
}

function getSessionKey(sessionId: string | undefined): string {
  return sessionId || DRAFT_SESSION_KEY
}

function getBottomScrollTop(container: HTMLDivElement): number {
  return Math.max(0, container.scrollHeight - container.clientHeight)
}

export const MessageList = memo(function MessageList({
  sessionId,
  messages,
  loading,
  sessionLoading,
  onRewindAndSend,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const scrollPositionsRef = useRef(new Map<string, number>())
  const activeSessionKeyRef = useRef(getSessionKey(sessionId))
  const stickToBottom = useRef(true)
  const previousMessageCount = useRef(0)

  const sessionKey = getSessionKey(sessionId)

  const saveScrollPosition = useCallback((sessionKey: string) => {
    const el = containerRef.current
    if (!el) return
    scrollPositionsRef.current.set(sessionKey, el.scrollTop)
    stickToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD
  }, [])

  const handleScroll = useCallback(() => {
    saveScrollPosition(activeSessionKeyRef.current)
  }, [saveScrollPosition])

  useLayoutEffect(() => {
    const sessionChanged = activeSessionKeyRef.current !== sessionKey
    const previousCount = previousMessageCount.current
    previousMessageCount.current = messages.length

    const container = containerRef.current
    if (!messages.length || !container) return

    if (sessionChanged) {
      activeSessionKeyRef.current = sessionKey
      const savedTop = scrollPositionsRef.current.get(sessionKey)
      const bottomTop = getBottomScrollTop(container)
      container.scrollTop =
        typeof savedTop === 'number' ? Math.min(savedTop, bottomTop) : bottomTop
      stickToBottom.current =
        container.scrollHeight - container.scrollTop - container.clientHeight <
        SCROLL_THRESHOLD
      return
    }

    if (!stickToBottom.current) return

    const bottomTop = getBottomScrollTop(container)
    if (loading || previousCount === 0) {
      container.scrollTop = bottomTop
    } else if (typeof container.scrollTo === 'function') {
      container.scrollTo({ top: bottomTop, behavior: 'smooth' })
    } else {
      container.scrollTop = bottomTop
    }
    saveScrollPosition(sessionKey)
  }, [loading, messages, saveScrollPosition, sessionKey])

  if (messages.length === 0) {
    if (sessionLoading) {
      return (
        <div className="flex flex-1 flex-col items-center justify-center p-8">
          <div className="font-mono text-xs text-muted-foreground/60">
            loading session
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
      className="flex-1 overflow-y-auto pb-4 pt-6 [overflow-anchor:none]"
    >
      <div className="mx-auto max-w-4xl max-md:max-w-none flex flex-col gap-6 max-md:gap-5">
        {messages.map((message, index) => {
          if (isCompactMarker(message)) {
            return <CompactMarker key={message.renderKey} />
          }
          return (
            <MessageBubble
              key={message.renderKey || `msg-${index}`}
              role={message.role}
              blocks={message.content}
              sourceIndex={message.sourceIndex}
              isStreaming={
                loading &&
                index === messages.length - 1 &&
                message.role === 'assistant'
              }
              isLoading={loading}
              totalTokens={message.meta?.total_tokens}
              model={message.meta?.model}
              contextWindow={message.meta?.context_window}
              onRewindAndSend={onRewindAndSend}
            />
          )
        })}
        <div className="h-4" />
      </div>
    </div>
  )
})
