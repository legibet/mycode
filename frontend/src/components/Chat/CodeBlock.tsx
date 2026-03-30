/**
 * Syntax-highlighted code block.
 * Language label and copy button float over code. No border.
 */

import { Check, Copy } from 'lucide-react'
import type { ComponentPropsWithoutRef } from 'react'
import { useEffect, useRef, useState } from 'react'
import type { ExtraProps } from 'react-markdown'
import { copyText } from '../../utils/clipboard'
import { cn } from '../../utils/cn'
import HighlightedCode from './HighlightedCode'

const LANGUAGE_RE = /language-([a-z0-9+#-]+)/i
type CodeBlockProps = ComponentPropsWithoutRef<'code'> & ExtraProps

export function CodeBlock({ className, children, ...props }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)
  const resetCopiedTimeoutRef = useRef<number | null>(null)

  const match = LANGUAGE_RE.exec(className || '')
  const language = match?.[1] ?? ''
  const rawContent = Array.isArray(children)
    ? children.join('')
    : String(children || '')
  const codeContent = rawContent.replace(/\n$/, '')

  const isInline = !match && !rawContent.endsWith('\n')

  useEffect(() => {
    return () => {
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current)
      }
    }
  }, [])

  const handleCopy = async () => {
    try {
      await copyText(codeContent)
      setCopied(true)
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current)
      }
      resetCopiedTimeoutRef.current = window.setTimeout(() => {
        setCopied(false)
        resetCopiedTimeoutRef.current = null
      }, 2000)
    } catch {
      /* ignore */
    }
  }

  if (isInline) {
    return (
      <code
        className={cn(
          'px-1.5 py-0.5 rounded bg-code font-mono text-[13px] text-accent font-medium',
          className,
        )}
        {...props}
      >
        {children}
      </code>
    )
  }

  return (
    <div
      data-code-block
      className="group/code relative my-3 rounded-md bg-code overflow-x-auto"
    >
      {language && (
        <span className="absolute top-1.5 left-3 text-[11px] font-mono text-muted-foreground/50 uppercase tracking-wider select-none">
          {language}
        </span>
      )}

      <button
        type="button"
        aria-label="Copy code"
        onClick={handleCopy}
        className={cn(
          'absolute top-1 right-1 z-10 flex items-center justify-center h-7 w-7 rounded-md transition duration-150',
          copied
            ? 'text-emerald-400 opacity-100'
            : 'text-muted-foreground/40 max-md:opacity-60 opacity-0 group-hover/code:opacity-100 hover:text-foreground/60 hover:bg-muted/20',
        )}
        title="Copy"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </button>

      <div className={cn('px-3 pb-2.5', language ? 'pt-6' : 'pt-2')}>
        <HighlightedCode language={language} code={codeContent} />
      </div>
    </div>
  )
}
