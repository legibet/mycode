import {
  type CodeToHastOptions,
  createBundledHighlighter,
  type HighlighterGeneric,
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
  golang: "go",
  objectivec: "objective-c",
  "objective-c++": "objective-cpp",
  plaintext: "text",
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

export function resolveLanguage(lang: string): ResolvedLanguage {
  const normalized = String(lang || "")
    .trim()
    .toLowerCase();

  if (!normalized) return "text";

  const resolved = LANGUAGE_ALIASES[normalized] || normalized;
  return Object.hasOwn(bundledLanguages, resolved)
    ? (resolved as BundledLanguage)
    : "text";
}

function loadLang(
  highlighter: AppHighlighter,
  lang: BundledLanguage,
): Promise<ResolvedLanguage> {
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

function codeToHtmlSafely(
  highlighter: AppHighlighter,
  code: string,
  options: CodeToHastOptions<string, AppTheme>,
): string | null {
  try {
    return highlighter.codeToHtml(code, options);
  } catch {
    return null;
  }
}

const SHIKI_OPTIONS = {
  themes: { dark: "dark-plus", light: "light-plus" },
  defaultColor: false,
} as const;

export async function highlightCode(
  code: string,
  language: string,
): Promise<string | null> {
  const resolvedLanguage = resolveLanguage(language);
  if (resolvedLanguage === "text") return null;

  const highlighter = await getHighlighter();
  const loadedLanguage = await loadLang(highlighter, resolvedLanguage);
  if (loadedLanguage === "text") return null;

  return codeToHtmlSafely(highlighter, code, {
    lang: loadedLanguage,
    ...SHIKI_OPTIONS,
  });
}
