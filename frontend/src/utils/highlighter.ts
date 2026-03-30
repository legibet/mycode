import { bundledLanguages, createHighlighter } from 'shiki'
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript'

let highlighterInstance = null

const LANGUAGE_ALIASES = {
  'c#': 'csharp',
  'c++': 'cpp',
  golang: 'go',
  md: 'markdown',
  plaintext: 'text',
  py: 'python',
  rb: 'ruby',
  rs: 'rust',
  sh: 'bash',
  text: 'text',
  ts: 'typescript',
  yml: 'yaml',
}

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

export function resolveLanguage(lang) {
  const normalized = String(lang || '')
    .trim()
    .toLowerCase()

  if (!normalized) return 'text'

  const resolved = LANGUAGE_ALIASES[normalized] || normalized
  return Object.hasOwn(bundledLanguages, resolved) ? resolved : 'text'
}

export function loadLang(highlighter, lang) {
  const resolved = resolveLanguage(lang)

  if (resolved === 'text') {
    return Promise.resolve('text')
  }

  if (highlighter.getLoadedLanguages().includes(resolved)) {
    return Promise.resolve(resolved)
  }

  if (!langLoadCache.has(resolved)) {
    try {
      langLoadCache.set(
        resolved,
        Promise.resolve(highlighter.loadLanguage(resolved))
          .then(() => resolved)
          .catch(() => {
            langLoadCache.delete(resolved)
            return 'text'
          }),
      )
    } catch {
      return Promise.resolve('text')
    }
  }

  return langLoadCache.get(resolved)
}

export function codeToHtmlSafely(highlighter, code, options) {
  try {
    return highlighter.codeToHtml(code, options)
  } catch {
    return null
  }
}

export const SHIKI_OPTIONS = {
  themes: { dark: 'dark-plus', light: 'light-plus' },
  defaultColor: false,
}
