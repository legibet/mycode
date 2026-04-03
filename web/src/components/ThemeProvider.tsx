/**
 * Theme context provider for light/dark/system modes.
 */

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'
import type { Theme, ThemeContextValue } from '../types'

interface ThemeProviderProps {
  children: ReactNode
  defaultTheme?: Theme
  storageKey?: string
}

const ThemeProviderContext = createContext<ThemeContextValue | null>(null)

function resolveTheme(theme: Theme): ThemeContextValue['resolvedTheme'] {
  if (theme !== 'system') return theme

  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light'
}

function applyTheme(theme: Theme): ThemeContextValue['resolvedTheme'] {
  const root = window.document.documentElement
  const resolvedTheme = resolveTheme(theme)

  root.classList.remove('light', 'dark')

  if (resolvedTheme === 'light') {
    root.classList.add('light')
  } else {
    root.classList.add('dark')
  }

  root.style.colorScheme = resolvedTheme

  return resolvedTheme
}

export function ThemeProvider({
  children,
  defaultTheme = 'system',
  storageKey = 'vite-ui-theme',
}: ThemeProviderProps) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const savedTheme = localStorage.getItem(storageKey)
    return savedTheme === 'light' ||
      savedTheme === 'dark' ||
      savedTheme === 'system'
      ? savedTheme
      : defaultTheme
  })
  const [resolvedTheme, setResolvedTheme] = useState<
    ThemeContextValue['resolvedTheme']
  >(() => resolveTheme(theme))

  useEffect(() => {
    setResolvedTheme(applyTheme(theme))
  }, [theme])

  useEffect(() => {
    if (theme !== 'system') return

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = () => {
      setResolvedTheme(applyTheme('system'))
    }

    mediaQuery.addEventListener('change', handleChange)
    return () => mediaQuery.removeEventListener('change', handleChange)
  }, [theme])

  const setTheme = useCallback(
    (nextTheme: Theme) => {
      localStorage.setItem(storageKey, nextTheme)
      setThemeState(nextTheme)
    },
    [storageKey],
  )

  const value = useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme, setTheme],
  )

  return (
    <ThemeProviderContext.Provider value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)

  if (!context) throw new Error('useTheme must be used within a ThemeProvider')

  return context
}
