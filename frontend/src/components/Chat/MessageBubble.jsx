/**
 * Message display.
 * No role labels — layout conveys who is speaking.
 * User: right-aligned compact bubble.
 * Assistant: left-aligned, full-width, content-first.
 */

import { Check, Copy } from 'lucide-react'
import { memo, useCallback, useMemo, useState } from 'react'
import { copyText } from '../../utils/clipboard'
import { cn } from '../../utils/cn'
import { MarkdownBlock } from './MarkdownBlock'
import { ReasoningBlock } from './ReasoningBlock'
import { ToolCard } from './ToolCard'

export const MessageBubble = memo(function MessageBubble({
  role,
  blocks,
  isStreaming,
  index,
}) {
  const isUser = role === 'user'
  const [copied, setCopied] = useState(false)

  const textContent = useMemo(
    () =>
      blocks
        .filter((block) => block?.type === 'text')
        .map((block) => block.text)
        .join('\n\n'),
    [blocks],
  )

  const handleCopy = useCallback(async () => {
    if (!textContent) return
    try {
      await copyText(textContent)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* ignore */
    }
  }, [textContent])

  if (isUser) {
    return (
      <div
        className="flex justify-end px-5 max-md:px-4 animate-fade-in-up"
        style={{ animationDelay: `${Math.min(index * 30, 150)}ms` }}
      >
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
            return (
              <ReasoningBlock
                key={block.renderKey || `thinking:${block.text || 'block'}`}
                content={block.text}
                isStreaming={isStreaming}
              />
            )
          }
          if (block.type === 'text') {
            return (
              <MarkdownBlock
                key={
                  block.renderKey || block.id || `text:${block.text || 'block'}`
                }
                content={block.text}
                isStreaming={isStreaming}
              />
            )
          }
          if (block.type === 'tool_use') {
            return (
              <ToolCard
                key={
                  block.renderKey || block.id || `tool:${block.name || 'tool'}`
                }
                name={block.name}
                args={block.input}
                output={block.runtime?.output}
                result={block.runtime?.result}
                pending={block.runtime?.pending}
                isError={block.runtime?.isError}
              />
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
            onClick={handleCopy}
            className={cn(
              'flex items-center justify-center h-6 w-6 rounded transition-all duration-150',
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
