import { use } from 'react'
import { createHighlighter } from 'shiki/bundle/web'
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript'

const highlighterPromise = createHighlighter({
  themes: ['dark-plus', 'light-plus'],
  langs: [
    'javascript',
    'typescript',
    'python',
    'json',
    'bash',
    'html',
    'css',
    'jsx',
    'tsx',
  ],
  engine: createJavaScriptRegexEngine(),
})

const langLoadCache = new Map()

function loadLang(highlighter, lang) {
  if (!langLoadCache.has(lang)) {
    langLoadCache.set(
      lang,
      highlighter
        .loadLanguage(lang)
        .then(() => lang)
        .catch(() => {
          langLoadCache.delete(lang)
          return null
        }),
    )
  }
  return langLoadCache.get(lang)
}

// Safety note: shiki codeToHtml generates HTML from a tokenized AST,
// producing only <pre>/<code>/<span> elements with inline styles.
// It does not pass through raw user input, so the output is safe.

export default function HighlightedCode({ code, language }) {
  const highlighter = use(highlighterPromise)

  const loaded = highlighter.getLoadedLanguages()
  let lang = language && loaded.includes(language) ? language : null

  if (!lang && language && language !== 'text' && language !== 'plaintext') {
    const result = loadLang(highlighter, language)
    if (result instanceof Promise) {
      const resolved = use(result)
      if (resolved) lang = resolved
    }
  }

  const html = highlighter.codeToHtml(code, {
    lang: lang || 'text',
    themes: { dark: 'dark-plus', light: 'light-plus' },
    defaultColor: false,
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
