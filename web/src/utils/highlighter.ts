import {
  type BundledLanguage,
  type BundledTheme,
  bundledLanguages,
  type CodeToHastOptions,
  createHighlighter,
  type HighlighterGeneric,
} from 'shiki'
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript'

export type AppHighlighter = HighlighterGeneric<BundledLanguage, BundledTheme>
export type ResolvedLanguage = BundledLanguage | 'text'

let highlighterInstance: AppHighlighter | null = null

const LANGUAGE_ALIASES: Record<string, string> = {
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

export function preloadHighlighter(): Promise<AppHighlighter> {
  return highlighterPromise
}

export function getHighlighter(): AppHighlighter | null {
  return highlighterInstance
}

const langLoadCache = new Map<ResolvedLanguage, Promise<ResolvedLanguage>>()

export function resolveLanguage(lang: string): ResolvedLanguage {
  const normalized = String(lang || '')
    .trim()
    .toLowerCase()

  if (!normalized) return 'text'

  const resolved = LANGUAGE_ALIASES[normalized] || normalized
  return Object.hasOwn(bundledLanguages, resolved)
    ? (resolved as BundledLanguage)
    : 'text'
}

export function loadLang(
  highlighter: AppHighlighter,
  lang: string,
): Promise<ResolvedLanguage> {
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
        Promise.resolve(highlighter.loadLanguage(resolved as BundledLanguage))
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

  return langLoadCache.get(resolved) ?? Promise.resolve('text')
}

export function codeToHtmlSafely(
  highlighter: AppHighlighter,
  code: string,
  options: CodeToHastOptions<ResolvedLanguage, BundledTheme>,
): string | null {
  try {
    return highlighter.codeToHtml(code, options)
  } catch {
    return null
  }
}

export const SHIKI_OPTIONS = {
  themes: { dark: 'dark-plus', light: 'light-plus' },
  defaultColor: false,
} as const
