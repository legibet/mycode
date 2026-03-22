/**
 * Syntax-highlighted code block.
 * Language label and copy button float over code. No border.
 */

import { Check, Copy } from 'lucide-react'
import { lazy, Suspense, useState } from 'react'
import { copyText } from '../../utils/clipboard'
import { cn } from '../../utils/cn'

const LANGUAGE_RE = /language-([a-z0-9+#-]+)/i
let highlightedCodePromise

function loadHighlightedCode() {
  if (!highlightedCodePromise) {
    highlightedCodePromise = import('./HighlightedCode')
  }

  return highlightedCodePromise
}

const HighlightedCode = lazy(loadHighlightedCode)

export function preloadHighlightedCode() {
  return loadHighlightedCode()
}

function HighlightedCodeFallback({ code }) {
  return (
    <pre
      className="m-0 overflow-x-auto whitespace-pre font-mono text-[13px] font-normal leading-[1.5] text-foreground"
      style={{ fontFamily: '"DM Mono", "JetBrains Mono", monospace' }}
    >
      <code>{code}</code>
    </pre>
  )
}

export function CodeBlock({ node, inline, className, children, ...props }) {
  const [copied, setCopied] = useState(false)

  const match = LANGUAGE_RE.exec(className || '')
  const language = match ? match[1] : ''
  const rawContent = Array.isArray(children)
    ? children.join('')
    : String(children || '')
  const codeContent = rawContent.replace(/\n$/, '')

  const isInline = inline || (!match && !rawContent.endsWith('\n'))

  const handleCopy = async () => {
    try {
      await copyText(codeContent)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
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
      <button
        type="button"
        onClick={handleCopy}
        className={cn(
          'absolute top-1 right-1 z-10 flex items-center justify-center h-7 w-7 rounded-md transition-all duration-150',
          copied
            ? 'text-emerald-400 opacity-100'
            : 'text-muted-foreground/40 opacity-0 group-hover/code:opacity-100 hover:text-foreground/60 hover:bg-muted/20',
        )}
        title="Copy"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </button>

      <div className="px-3 pt-2 pb-2.5">
        {language && (
          <div className="mb-1">
            <span className="text-[11px] font-mono text-muted-foreground/30 uppercase tracking-wider select-none">
              {language}
            </span>
          </div>
        )}

        <Suspense fallback={<HighlightedCodeFallback code={codeContent} />}>
          <HighlightedCode language={language} code={codeContent} />
        </Suspense>
      </div>
    </div>
  )
}
