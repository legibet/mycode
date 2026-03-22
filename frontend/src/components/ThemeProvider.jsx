/**
 * Theme context provider for light/dark/system modes.
 */

import { createContext, useContext, useEffect, useState } from 'react'

const ThemeProviderContext = createContext({
  theme: 'system',
  setTheme: () => null,
})

function applyTheme(theme) {
  const root = window.document.documentElement
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches

  root.classList.remove('light', 'dark')

  if (theme === 'light' || (theme === 'system' && !prefersDark)) {
    root.classList.add('light')
  }
}

export function ThemeProvider({
  children,
  defaultTheme = 'system',
  storageKey = 'vite-ui-theme',
  ...props
}) {
  const [theme, setTheme] = useState(() => {
    return localStorage.getItem(storageKey) || defaultTheme
  })

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  useEffect(() => {
    if (theme !== 'system') return

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = () => applyTheme('system')

    mediaQuery.addEventListener('change', handleChange)
    return () => mediaQuery.removeEventListener('change', handleChange)
  }, [theme])

  const value = {
    theme,
    setTheme: (theme) => {
      localStorage.setItem(storageKey, theme)
      setTheme(theme)
    },
  }

  return (
    <ThemeProviderContext.Provider {...props} value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)

  if (context === undefined)
    throw new Error('useTheme must be used within a ThemeProvider')

  return context
}
