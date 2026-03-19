/**
 * Message block with role label.
 * Flat, content-first. Copy button for assistant text on hover.
 */

import { Check, Copy } from 'lucide-react'
import { useCallback, useState } from 'react'
import { cn } from '../../utils/cn'
import { MarkdownBlock } from './MarkdownBlock'
import { ReasoningBlock } from './ReasoningBlock'
import { ToolCard } from './ToolCard'

function copyText(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text)
  }
  const el = document.createElement('textarea')
  el.value = text
  el.style.cssText = 'position:fixed;opacity:0'
  document.body.appendChild(el)
  el.select()
  document.execCommand('copy')
  document.body.removeChild(el)
  return Promise.resolve()
}

export function MessageBubble({ role, blocks, isStreaming, index }) {
  const isUser = role === 'user'
  const [copied, setCopied] = useState(false)

  const textContent = blocks
    .filter((block) => block?.type === 'text')
    .map((block) => block.text)
    .join('\n\n')

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

  return (
    <div
      className="group/msg relative px-5 max-md:px-4 animate-fade-in-up"
      style={{ animationDelay: `${Math.min(index * 30, 150)}ms` }}
    >
      {/* Role label */}
      <div className="mb-1.5">
        <span
          className={cn(
            'font-mono text-2xs uppercase tracking-widest',
            isUser ? 'text-muted-foreground/60' : 'text-accent/70',
          )}
        >
          {isUser ? 'you' : 'assistant'}
        </span>
      </div>

      {/* Content — gap-3 (12px) between blocks */}
      <div className="flex flex-col gap-3 text-foreground/90 leading-relaxed text-sm">
        {blocks.map((block, i) => {
          if (block.type === 'thinking') {
            return (
              <ReasoningBlock
                key={`reasoning-${i}`}
                content={block.text}
                isStreaming={isStreaming}
              />
            )
          }
          if (block.type === 'text') {
            return (
              <MarkdownBlock
                key={block.id || `text-${i}`}
                content={block.text}
              />
            )
          }
          if (block.type === 'tool_use') {
            return (
              <ToolCard
                key={block.id || `tool-${i}`}
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

      {/* Copy button — bottom of message, hover to show */}
      {!isUser && textContent && !isStreaming && (
        <div className="mt-2 opacity-0 group-hover/msg:opacity-100 transition-opacity duration-150">
          <button
            type="button"
            onClick={handleCopy}
            className={cn(
              'flex items-center justify-center h-6 w-6 rounded transition-all duration-150',
              copied
                ? 'text-emerald-400'
                : 'text-muted-foreground/30 hover:text-muted-foreground/60',
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
}
