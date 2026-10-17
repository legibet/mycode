import { startTransition, useEffect, useState } from "react";

// Safety note: shiki codeToHtml generates HTML from a tokenized AST,
// producing only <pre>/<code>/<span> elements with inline styles.
// It does not pass through raw user input, so the output is safe.

// Start the shiki chunk download at module-eval, not on first effect.
const highlighterModulePromise = import("@/utils/highlighter");

// Preflight leaves <pre> bare and sizes its <code> at 1em, so this one class
// list types both the plain fallback and shiki's <pre class="shiki">.
const CODE_TYPE = "font-mono text-ui leading-relaxed";

interface HighlightedCodeProps {
  code: string;
  language: string;
}

export function HighlightedCode({ code, language }: HighlightedCodeProps) {
  const [highlight, setHighlight] = useState<{
    code: string;
    language: string;
    html: string;
  } | null>(null);

  useEffect(() => {
    // No setHighlight(null) needed: the render guard below ignores any
    // highlight whose code/language no longer match the current props.
    if (!language.trim()) return;

    let cancelled = false;
    void highlighterModulePromise
      .then(({ highlightCode }) => highlightCode(code, language))
      .then((html) => {
        if (cancelled) return;
        startTransition(() => {
          setHighlight(html ? { code, language, html } : null);
        });
      })
      .catch(() => {
        if (!cancelled) setHighlight(null);
      });

    return () => {
      cancelled = true;
    };
  }, [code, language]);

  const html = highlight?.code === code && highlight.language === language ? highlight.html : null;

  if (!html) {
    return (
      <pre className={CODE_TYPE}>
        <code>{code}</code>
      </pre>
    );
  }

  return <div className={CODE_TYPE} dangerouslySetInnerHTML={{ __html: html }} />;
}
