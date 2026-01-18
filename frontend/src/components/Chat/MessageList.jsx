import { useEffect, useRef } from 'react'
import { MessageBubble } from './MessageBubble'

export function MessageList({ messages, loading }) {
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  })

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8 text-center text-muted-foreground">
        <div className="max-w-md space-y-4">
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">How can I help you today?</h1>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto pb-4 pt-4">
      <div className="mx-auto max-w-3xl flex flex-col gap-6">
        {messages.map((message, index) => (
          <MessageBubble
            key={`${message.role}-${index}-${message.parts.length}`}
            role={message.role}
            parts={message.parts}
            isStreaming={loading && index === messages.length - 1 && message.role === 'assistant'}
          />
        ))}
        <div ref={endRef} className="h-4" />
      </div>
    </div>
  )
}
