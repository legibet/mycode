/**
 * Theme context provider for light/dark/system modes.
 */

import { createContext, useContext, useEffect, useState } from 'react'

const ThemeProviderContext = createContext({
  theme: 'system',
  setTheme: () => null,
})

export function ThemeProvider({ children, defaultTheme = 'system', storageKey = 'vite-ui-theme', ...props }) {
  const [theme, setTheme] = useState(() => {
    return localStorage.getItem(storageKey) || defaultTheme
  })

  useEffect(() => {
    const root = window.document.documentElement

    // Our CSS: :root = dark (default), .light = light theme
    // Only add 'light' class when needed; dark needs no class
    root.classList.remove('light', 'dark')

    if (theme === 'system') {
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      if (!prefersDark) root.classList.add('light')
      return
    }

    if (theme === 'light') {
      root.classList.add('light')
    }
    // dark = no class needed, :root handles it
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

  if (context === undefined) throw new Error('useTheme must be used within a ThemeProvider')

  return context
}
