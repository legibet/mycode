/**
 * Syntax-highlighted code block with copy functionality.
 */

import { Check, Copy, Terminal } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vs, vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { cn } from '../../utils/cn'
import { useTheme } from '../ThemeProvider'

export function CodeBlock({ node, inline, className, children, ...props }) {
  const { theme } = useTheme()
  const [copied, setCopied] = useState(false)

  const isDark = theme === 'dark' || (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)

  // Clean themes once to avoid perf hit (though negligible here)
  const syntaxTheme = useMemo(() => {
    const baseTheme = isDark ? vscDarkPlus : vs
    // We clone and strip background properties to let our container control the bg
    // and to avoid the React warning about mixed shorthand/longhand styles
    const clean = { ...baseTheme }

    // Explicitly strip background from the top-level selectors usually added by Prism
    if (clean['pre[class*="language-"]']) {
      const { background, backgroundColor, ...rest } = clean['pre[class*="language-"]']
      clean['pre[class*="language-"]'] = rest
    }

    return clean
  }, [isDark])

  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const rawContent = Array.isArray(children) ? children.join('') : String(children || '')
  const codeContent = rawContent.replace(/\n$/, '')

  // Robust inline detection:
  // 1. Trust 'inline' prop if provided as true
  // 2. Fallback: If no language set and content doesn't end with newline (common in react-markdown for inline), treat as inline
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

  // Inline code styling
  if (isInline) {
    return (
      <code
        className={cn('px-1.5 py-0.5 rounded bg-code font-mono text-[13px] font-medium text-foreground', className)}
        {...props}
      >
        {children}
      </code>
    )
  }

  // Block code styling
  return (
    <div className="group relative my-4 overflow-hidden rounded-lg border border-code-border bg-code">
      {/* Header Bar */}
      <div className="flex items-center justify-between border-b border-code-border bg-code-header px-3 py-2">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 opacity-70">
            <Terminal className="h-3.5 w-3.5" />
            <span className="text-xs font-medium font-mono lowercase select-none">{language || 'text'}</span>
          </div>
        </div>

        <button
          type="button"
          onClick={handleCopy}
          className={cn(
            'flex items-center gap-1.5 rounded px-2 py-1 text-xs transition-all duration-200',
            copied
              ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'text-muted-foreground hover:bg-background hover:text-foreground'
          )}
          title="Copy code"
        >
          {copied ? (
            <>
              <Check className="h-3.5 w-3.5" />
              <span className="font-medium">Copied</span>
            </>
          ) : (
            <>
              <Copy className="h-3.5 w-3.5" />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>

      {/* Code Area */}
      <div className="relative overflow-x-auto p-4">
        <SyntaxHighlighter
          style={syntaxTheme}
          language={language}
          PreTag="div"
          showLineNumbers={true}
          lineNumberStyle={{
            minWidth: '2.5em',
            paddingRight: '1em',
            color: isDark ? 'rgba(128, 128, 128, 0.4)' : 'rgba(128, 128, 128, 0.5)',
            textAlign: 'right',
            userSelect: 'none',
            fontWeight: 400,
          }}
          customStyle={{
            margin: 0,
            padding: 0,
            background: 'transparent',
            fontSize: '13px',
            lineHeight: '1.6',
            fontWeight: isDark ? 400 : 500,
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
