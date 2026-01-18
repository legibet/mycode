import { Bot, User } from 'lucide-react'
import { cn } from '../../utils/cn'
import { MarkdownBlock } from './MarkdownBlock'
import { ToolCard } from './ToolCard'

export function MessageBubble({ role, parts }) {
  const isUser = role === 'user'

  return (
    <div className={cn('group relative flex gap-4 px-4 py-2 w-full', isUser ? '' : '')}>
      <div
        className={cn(
          'flex h-8 w-8 shrink-0 select-none items-center justify-center rounded-full border shadow-sm',
          isUser ? 'bg-background text-foreground' : 'bg-primary text-primary-foreground border-primary'
        )}
      >
        {isUser ? <User className="h-5 w-5" /> : <Bot className="h-5 w-5" />}
      </div>

      <div className="min-w-0 flex-1 space-y-1 overflow-hidden pt-1">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-sm text-foreground">{isUser ? 'You' : 'Assistant'}</span>
        </div>

        <div className="space-y-4 text-foreground/90 leading-relaxed">
          {parts.map((part, index) => {
            if (part.type === 'text') {
              return <MarkdownBlock key={part.id || `text-${index}`} content={part.content} />
            }
            if (part.type === 'tool') {
              return (
                <ToolCard
                  key={part.id || `tool-${index}`}
                  name={part.name}
                  args={part.args}
                  result={part.result}
                  pending={part.pending}
                />
              )
            }
            return null
          })}
        </div>
      </div>
    </div>
  )
}
