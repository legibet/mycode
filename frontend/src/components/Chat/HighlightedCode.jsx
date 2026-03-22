import { useMemo } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vs, vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useTheme } from '../ThemeProvider'

const THEME_SELECTORS = ['pre[class*="language-"]', 'code[class*="language-"]']

export default function HighlightedCode({ code, language }) {
  const { theme } = useTheme()

  const isLight =
    theme === 'light' ||
    (theme === 'system' &&
      !window.matchMedia('(prefers-color-scheme: dark)').matches)

  const syntaxTheme = useMemo(() => {
    const baseTheme = isLight ? vs : vscDarkPlus
    const cleanTheme = { ...baseTheme }

    for (const selector of THEME_SELECTORS) {
      if (!cleanTheme[selector]) continue
      const { color } = cleanTheme[selector]
      cleanTheme[selector] = color ? { color } : {}
    }

    return cleanTheme
  }, [isLight])

  return (
    <SyntaxHighlighter
      style={syntaxTheme}
      language={language || undefined}
      PreTag="div"
      showLineNumbers={false}
      customStyle={{
        margin: 0,
        padding: 0,
        background: 'transparent',
        fontFamily: '"DM Mono", "JetBrains Mono", monospace',
        fontSize: '13px',
        lineHeight: '1.5',
        fontWeight: 400,
      }}
      codeTagProps={{
        style: {
          fontFamily: 'inherit',
          fontWeight: 'inherit',
        },
      }}
    >
      {code}
    </SyntaxHighlighter>
  )
}
