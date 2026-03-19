/**
 * Syntax-highlighted code block.
 * Language label and copy button float over code. No border.
 */

import { Check, Copy } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vs, vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { cn } from '../../utils/cn'
import { useTheme } from '../ThemeProvider'

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

export function CodeBlock({ node, inline, className, children, ...props }) {
  const { theme } = useTheme()
  const [copied, setCopied] = useState(false)

  const isLight =
    theme === 'light' ||
    (theme === 'system' &&
      !window.matchMedia('(prefers-color-scheme: dark)').matches)
  const isDark = !isLight

  const syntaxTheme = useMemo(() => {
    const baseTheme = isDark ? vscDarkPlus : vs
    const clean = { ...baseTheme }
    // Only keep color from pre/code selectors, strip everything else
    for (const selector of [
      'pre[class*="language-"]',
      'code[class*="language-"]',
    ]) {
      if (clean[selector]) {
        const { color } = clean[selector]
        clean[selector] = color ? { color } : {}
      }
    }
    return clean
  }, [isDark])

  const match = /language-(\w+)/.exec(className || '')
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

        <SyntaxHighlighter
          style={syntaxTheme}
          language={language}
          PreTag="div"
          showLineNumbers={false}
          customStyle={{
            margin: 0,
            padding: 0,
            background: 'transparent',
            fontFamily: '"DM Mono", "JetBrains Mono", monospace',
            fontSize: '13px',
            lineHeight: '1.5',
            fontWeight: 400,
          }}
          codeTagProps={{
            style: {
              fontFamily: 'inherit',
              fontWeight: 'inherit',
            },
          }}
        >
          {codeContent}
        </SyntaxHighlighter>
      </div>
    </div>
  )
}
