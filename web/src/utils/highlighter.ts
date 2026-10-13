import {
  createBundledHighlighter,
  type HighlighterGeneric,
  type ThemedToken,
  type ThemeInput,
} from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import { type BundledLanguage, bundledLanguages } from "shiki/langs";

type AppTheme = "dark-plus" | "light-plus";
type AppHighlighter = HighlighterGeneric<BundledLanguage, AppTheme>;
type ResolvedLanguage = BundledLanguage | "text";

const themes = {
  "dark-plus": () => import("@shikijs/themes/dark-plus"),
  "light-plus": () => import("@shikijs/themes/light-plus"),
} satisfies Record<AppTheme, ThemeInput>;

const createHighlighter = createBundledHighlighter<BundledLanguage, AppTheme>({
  langs: bundledLanguages,
  themes,
  engine: () => createJavaScriptRegexEngine(),
});

let highlighterPromise: Promise<AppHighlighter> | null = null;
const langLoadCache = new Map<ResolvedLanguage, Promise<ResolvedLanguage>>();

const LANGUAGE_ALIASES: Record<string, string> = {
  cc: "cpp",
  cxx: "cpp",
  golang: "go",
  h: "c",
  hh: "cpp",
  hpp: "cpp",
  objectivec: "objective-c",
  "objective-c++": "objective-cpp",
  plaintext: "text",
  pyi: "python",
  vuejs: "vue",
};

function getHighlighter(): Promise<AppHighlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighter({
      themes: ["dark-plus", "light-plus"],
      langs: [],
    });
  }
  return highlighterPromise;
}

// Pre-warm so the first highlight only waits on language load.
void getHighlighter();

function resolveLanguage(lang: string): ResolvedLanguage {
  const normalized = String(lang || "")
    .trim()
    .toLowerCase();

  if (!normalized) return "text";

  const resolved = LANGUAGE_ALIASES[normalized] || normalized;
  return Object.hasOwn(bundledLanguages, resolved) ? (resolved as BundledLanguage) : "text";
}

function loadLang(highlighter: AppHighlighter, lang: BundledLanguage): Promise<ResolvedLanguage> {
  if (highlighter.getLoadedLanguages().includes(lang)) {
    return Promise.resolve(lang);
  }

  if (!langLoadCache.has(lang)) {
    try {
      langLoadCache.set(
        lang,
        Promise.resolve(highlighter.loadLanguage(lang))
          .then(() => lang)
          .catch(() => {
            langLoadCache.delete(lang);
            return "text";
          }),
      );
    } catch {
      return Promise.resolve("text");
    }
  }

  return langLoadCache.get(lang) ?? Promise.resolve("text");
}

const SHIKI_OPTIONS = {
  themes: { dark: "dark-plus", light: "light-plus" },
  defaultColor: false,
} as const;

async function loadHighlighter(
  language: string,
): Promise<[AppHighlighter, BundledLanguage] | null> {
  const resolvedLanguage = resolveLanguage(language);
  if (resolvedLanguage === "text") return null;

  const highlighter = await getHighlighter();
  const loadedLanguage = await loadLang(highlighter, resolvedLanguage);
  return loadedLanguage === "text" ? null : [highlighter, loadedLanguage];
}

export async function highlightCode(code: string, language: string): Promise<string | null> {
  const loaded = await loadHighlighter(language);
  if (!loaded) return null;
  const [highlighter, lang] = loaded;
  try {
    return highlighter.codeToHtml(code, { lang, ...SHIKI_OPTIONS });
  } catch {
    return null;
  }
}

/**
 * Tokens per line. Each token's `htmlStyle` carries the same `--shiki-*`
 * variables as `highlightCode`, so it renders under a `.shiki` ancestor.
 * `context` is code assumed to precede `code`; it sets the starting grammar
 * state (e.g. inside a docstring) and is left out of the result.
 */
export async function highlightTokens(
  code: string,
  language: string,
  context = "",
): Promise<ThemedToken[][] | null> {
  const loaded = await loadHighlighter(language);
  if (!loaded) return null;
  const [highlighter, lang] = loaded;
  try {
    return highlighter.codeToTokens(code, {
      lang,
      grammarContextCode: context,
      ...SHIKI_OPTIONS,
    }).tokens;
  } catch {
    return null;
  }
}
