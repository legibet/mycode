import { cn } from '../../utils/cn'
import { MarkdownBlock } from './MarkdownBlock'
import { ToolCard } from './ToolCard'

export function MessageBubble({ role, parts }) {
  const isUser = role === 'user'

  return (
    <div className={cn('group relative flex gap-4 px-4 py-6 md:px-0 w-full', isUser ? '' : '')}>
      <div
        className={cn(
          'flex h-8 w-8 shrink-0 select-none items-center justify-center rounded-sm border shadow-sm',
          isUser ? 'bg-background text-foreground' : 'bg-primary text-primary-foreground border-primary'
        )}
      >
        {isUser ? 'Y' : 'AI'}
      </div>

      <div className="min-w-0 flex-1 space-y-2 overflow-hidden">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-sm text-foreground">{isUser ? 'You' : 'Assistant'}</span>
        </div>

        <div className="space-y-4 text-foreground/90">
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
