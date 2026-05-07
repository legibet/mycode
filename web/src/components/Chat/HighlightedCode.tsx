import { startTransition, useEffect, useState } from 'react'
import type { InlineStyle } from '../../types'

// Safety note: shiki codeToHtml generates HTML from a tokenized AST,
// producing only <pre>/<code>/<span> elements with inline styles.
// It does not pass through raw user input, so the output is safe.

const MONO_STYLE = {
  margin: 0,
  padding: 0,
  fontFamily: '"JetBrains Mono", ui-monospace, monospace',
  fontSize: '13px',
  lineHeight: '1.5',
  fontWeight: 400,
} satisfies InlineStyle

interface HighlightedCodeProps {
  code: string
  language: string
}

export default function HighlightedCode({
  code,
  language,
}: HighlightedCodeProps) {
  const [highlight, setHighlight] = useState<{
    code: string
    language: string
    html: string
  } | null>(null)

  useEffect(() => {
    if (!language.trim()) {
      setHighlight(null)
      return
    }

    let cancelled = false
    void import('../../utils/highlighter')
      .then(({ highlightCode }) => highlightCode(code, language))
      .then((html) => {
        if (cancelled) return
        startTransition(() => {
          setHighlight(html ? { code, language, html } : null)
        })
      })
      .catch(() => {
        if (!cancelled) setHighlight(null)
      })

    return () => {
      cancelled = true
    }
  }, [code, language])

  const html =
    highlight?.code === code && highlight.language === language
      ? highlight.html
      : null

  if (!html) {
    return (
      <pre style={MONO_STYLE}>
        <code>{code}</code>
      </pre>
    )
  }

  return (
    <div
      className="shiki-wrapper"
      style={MONO_STYLE}
      // biome-ignore lint/security/noDangerouslySetInnerHtml: shiki output is from tokenized AST, not user input
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
