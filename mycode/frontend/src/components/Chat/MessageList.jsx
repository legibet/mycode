/**
 * Scrollable message list with auto-scroll.
 * Empty state: blinking cursor terminal prompt.
 */

import { useEffect, useRef } from 'react'
import { MessageBubble } from './MessageBubble'

export function MessageList({ messages, loading }) {
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  })

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8">
        <div className="space-y-3 text-center">
          <h1 className="font-display text-3xl tracking-tighter text-foreground/80">
            mycode
            <span className="inline-block w-[2px] h-7 bg-accent ml-0.5 align-middle animate-cursor-blink" />
          </h1>
          <p className="text-xs font-mono text-muted-foreground/50 tracking-wide">
            ready.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto pb-4 pt-6">
      <div className="mx-auto max-w-4xl flex flex-col gap-1">
        {messages.map((message, index) => (
          <MessageBubble
            key={`${message.role}-${index}-${message.parts.length}`}
            role={message.role}
            parts={message.parts}
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
