/**
 * Scrollable message list with auto-scroll.
 * Only auto-scrolls when the user is already near the bottom.
 * Empty state: blinking cursor terminal prompt.
 */

import { useCallback, useEffect, useRef } from 'react'
import { MessageBubble } from './MessageBubble'

const SCROLL_THRESHOLD = 120

export function MessageList({ messages, loading }) {
  const containerRef = useRef(null)
  const endRef = useRef(null)
  const stickToBottom = useRef(true)

  const handleScroll = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    stickToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD
  }, [])

  useEffect(() => {
    if (stickToBottom.current) {
      endRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  })

  if (messages.length === 0) {
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
            key={
              message.renderKey || `${message.role}:${message.content.length}`
            }
            role={message.role}
            blocks={message.content}
            isStreaming={
              loading &&
              index === messages.length - 1 &&
              message.role === 'assistant'
            }
            index={index}
          />
        ))}
        <div ref={endRef} className="h-4" />
      </div>
    </div>
  )
}
