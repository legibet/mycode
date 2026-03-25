import { createHighlighter } from 'shiki/bundle/web'
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript'

let highlighterInstance = null

export const highlighterPromise = createHighlighter({
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
}).then((highlighter) => {
  highlighterInstance = highlighter
  return highlighter
})

export function preloadHighlighter() {
  return highlighterPromise
}

export function getHighlighter() {
  return highlighterInstance
}

const langLoadCache = new Map()

export function loadLang(highlighter, lang) {
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

export const SHIKI_OPTIONS = {
  themes: { dark: 'dark-plus', light: 'light-plus' },
  defaultColor: false,
}
