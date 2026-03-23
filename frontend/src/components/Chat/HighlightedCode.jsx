import { startTransition, useEffect, useState } from 'react'
import {
  getHighlighter,
  loadLang,
  SHIKI_OPTIONS,
} from '../../utils/highlighter'

// Safety note: shiki codeToHtml generates HTML from a tokenized AST,
// producing only <pre>/<code>/<span> elements with inline styles.
// It does not pass through raw user input, so the output is safe.

export default function HighlightedCode({ code, language }) {
  const highlighter = getHighlighter()
  const loadedLanguages = highlighter.getLoadedLanguages()
  const immediateLanguage =
    language && loadedLanguages.includes(language) ? language : null
  const [resolvedLanguage, setResolvedLanguage] = useState(immediateLanguage)

  useEffect(() => {
    const nextLanguage =
      language && highlighter.getLoadedLanguages().includes(language)
        ? language
        : null

    setResolvedLanguage((current) =>
      current === nextLanguage ? current : nextLanguage,
    )

    if (
      nextLanguage ||
      !language ||
      language === 'text' ||
      language === 'plaintext'
    ) {
      return
    }

    let cancelled = false

    void loadLang(highlighter, language).then((loadedLanguage) => {
      if (cancelled || !loadedLanguage) {
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
  }, [highlighter, language])

  const html = highlighter.codeToHtml(code, {
    lang: resolvedLanguage || 'text',
    ...SHIKI_OPTIONS,
  })

  return (
    <div
      className="shiki-wrapper"
      style={{
        margin: 0,
        padding: 0,
        fontFamily: '"DM Mono", "JetBrains Mono", monospace',
        fontSize: '13px',
        lineHeight: '1.5',
        fontWeight: 400,
      }}
      // biome-ignore lint/security/noDangerouslySetInnerHtml: shiki output is from tokenized AST, not user input
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
