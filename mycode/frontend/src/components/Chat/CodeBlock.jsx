/**
 * Syntax-highlighted code block with copy button.
 * Adapted for ink-on-paper theme.
 */

import { Check, Copy } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vs, vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { cn } from '../../utils/cn'
import { useTheme } from '../ThemeProvider'

export function CodeBlock({ node, inline, className, children, ...props }) {
  const { theme } = useTheme()
  const [copied, setCopied] = useState(false)

  // Dark is default (:root); light only when explicitly set or system prefers light
  const isLight =
    theme === 'light' ||
    (theme === 'system' &&
      !window.matchMedia('(prefers-color-scheme: dark)').matches)
  const isDark = !isLight

  const syntaxTheme = useMemo(() => {
    const baseTheme = isDark ? vscDarkPlus : vs
    const clean = { ...baseTheme }
    // Strip layout/font properties from theme — we control these via customStyle
    const stripKeys = [
      'background',
      'backgroundColor',
      'fontFamily',
      'fontSize',
      'lineHeight',
    ]
    for (const selector of [
      'pre[class*="language-"]',
      'code[class*="language-"]',
    ]) {
      if (clean[selector]) {
        const filtered = { ...clean[selector] }
        for (const key of stripKeys) delete filtered[key]
        clean[selector] = filtered
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
      await navigator.clipboard.writeText(codeContent)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      console.error('Failed to copy:', err)
    }
  }

  if (isInline) {
    return (
      <code
        className={cn(
          'px-1.5 py-0.5 rounded bg-code font-mono text-[13px] text-accent',
          className,
        )}
        {...props}
      >
        {children}
      </code>
    )
  }

  return (
    <div className="group relative my-3 overflow-hidden rounded-md border border-code-border bg-code">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-code-border bg-code-header px-3 py-1.5">
        <span className="text-2xs font-mono text-muted-foreground/50 lowercase select-none tracking-wide">
          {language || 'text'}
        </span>

        <button
          type="button"
          onClick={handleCopy}
          className={cn(
            'flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-mono transition-all duration-200',
            copied
              ? 'text-emerald-400'
              : 'text-muted-foreground/40 hover:text-foreground/60',
          )}
          title="Copy code"
        >
          {copied ? (
            <>
              <Check className="h-3 w-3" />
              <span>copied</span>
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" />
              <span>copy</span>
            </>
          )}
        </button>
      </div>

      {/* Code */}
      <div className="relative overflow-x-auto p-3">
        <SyntaxHighlighter
          style={syntaxTheme}
          language={language}
          PreTag="div"
          showLineNumbers={true}
          lineNumberStyle={{
            minWidth: '2em',
            paddingRight: '1em',
            color: isDark
              ? 'rgba(128, 128, 128, 0.25)'
              : 'rgba(128, 128, 128, 0.35)',
            textAlign: 'right',
            userSelect: 'none',
            fontWeight: 400,
          }}
          customStyle={{
            margin: 0,
            padding: 0,
            background: 'transparent',
            fontFamily: '"DM Mono", "JetBrains Mono", monospace',
            fontSize: '13px',
            lineHeight: '1.6',
            fontWeight: 400,
            display: 'grid',
            gridTemplateColumns: 'auto 1fr',
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
