/**
 * Message bubble with left border role indicator.
 * No avatars — minimal, content-first design.
 */

import { cn } from '../../utils/cn'
import { MarkdownBlock } from './MarkdownBlock'
import { ToolCard } from './ToolCard'
import { ReasoningBlock } from './ReasoningBlock'

export function MessageBubble({ role, parts, isStreaming, index }) {
  const isUser = role === 'user'

  return (
    <div
      className={cn('group relative px-6 py-4 animate-fade-in-up', isUser ? '' : '')}
      style={{ animationDelay: `${Math.min(index * 30, 150)}ms` }}
    >
      {/* Left border indicator */}
      <div
        className={cn(
          'absolute left-0 top-4 bottom-4 w-[2px] rounded-r transition-colors',
          isUser ? 'bg-border' : 'bg-accent/60'
        )}
      />

      <div className="ml-4 min-w-0">
        {/* Role label */}
        <div className="mb-2">
          <span
            className={cn(
              'font-mono text-2xs uppercase tracking-widest',
              isUser ? 'text-muted-foreground/60' : 'text-accent/70'
            )}
          >
            {isUser ? 'you' : 'assistant'}
          </span>
        </div>

        {/* Content */}
        <div className="space-y-3 text-foreground/90 leading-relaxed text-sm">
          {parts.map((part, i) => {
            if (part.type === 'reasoning') {
              return <ReasoningBlock key={`reasoning-${i}`} content={part.content} isStreaming={isStreaming} />
            }
            if (part.type === 'text') {
              return <MarkdownBlock key={part.id || `text-${i}`} content={part.content} />
            }
            if (part.type === 'tool') {
              return (
                <ToolCard
                  key={part.id || `tool-${i}`}
                  name={part.name}
                  args={part.args}
                  result={part.result}
                  pending={part.pending}
                />
              )
            }
            return null
          })}

          {/* Streaming cursor */}
          {isStreaming && (
            <span className="inline-block w-[2px] h-4 bg-accent animate-cursor-blink ml-0.5 align-middle" />
          )}
        </div>
      </div>
    </div>
  )
}
