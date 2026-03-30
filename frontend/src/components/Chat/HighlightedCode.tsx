import { startTransition, useEffect, useState } from 'react'
import {
  codeToHtmlSafely,
  getHighlighter,
  loadLang,
  resolveLanguage,
  SHIKI_OPTIONS,
} from '../../utils/highlighter'

// Safety note: shiki codeToHtml generates HTML from a tokenized AST,
// producing only <pre>/<code>/<span> elements with inline styles.
// It does not pass through raw user input, so the output is safe.

const MONO_STYLE = {
  margin: 0,
  padding: 0,
  fontFamily: '"DM Mono", "JetBrains Mono", monospace',
  fontSize: '13px',
  lineHeight: '1.5',
  fontWeight: 400,
}

export default function HighlightedCode({ code, language }) {
  const highlighter = getHighlighter()
  const targetLanguage = resolveLanguage(language)
  const loadedLanguages = highlighter?.getLoadedLanguages()
  const immediateLanguage = loadedLanguages?.includes(targetLanguage)
    ? targetLanguage
    : 'text'
  const [resolvedLanguage, setResolvedLanguage] = useState(immediateLanguage)

  useEffect(() => {
    if (!highlighter) return

    const nextLanguage = highlighter
      .getLoadedLanguages()
      .includes(targetLanguage)
      ? targetLanguage
      : 'text'

    setResolvedLanguage((current) =>
      current === nextLanguage ? current : nextLanguage,
    )

    if (nextLanguage !== 'text' || targetLanguage === 'text') {
      return
    }

    let cancelled = false

    void loadLang(highlighter, targetLanguage).then((loadedLanguage) => {
      if (cancelled || loadedLanguage === 'text') {
        return
      }

      startTransition(() => {
        setResolvedLanguage((current) =>
          current === loadedLanguage ? current : loadedLanguage,
        )
      })
    })

    return () => {
      cancelled = true
    }
  }, [highlighter, targetLanguage])

  if (!highlighter) {
    return (
      <pre style={MONO_STYLE}>
        <code>{code}</code>
      </pre>
    )
  }

  const html = codeToHtmlSafely(highlighter, code, {
    lang: resolvedLanguage,
    ...SHIKI_OPTIONS,
  })

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
