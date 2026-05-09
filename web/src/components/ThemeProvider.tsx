/**
 * Theme context provider for light/dark/system modes.
 */

import {
  createContext,
  type ReactNode,
  use,
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import type { Theme, ThemeContextValue } from "../types";

interface ThemeProviderProps {
  children: ReactNode;
  defaultTheme?: Theme;
  storageKey?: string;
}

const ThemeProviderContext = createContext<ThemeContextValue | null>(null);

const DARK_QUERY = "(prefers-color-scheme: dark)";

function subscribeToColorScheme(onChange: () => void) {
  const mq = window.matchMedia(DARK_QUERY);
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}

function getColorSchemeSnapshot() {
  return window.matchMedia(DARK_QUERY).matches;
}

export function ThemeProvider({
  children,
  defaultTheme = "system",
  storageKey = "vite-ui-theme",
}: ThemeProviderProps) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const savedTheme = localStorage.getItem(storageKey);
    return savedTheme === "light" ||
      savedTheme === "dark" ||
      savedTheme === "system"
      ? savedTheme
      : defaultTheme;
  });
  const systemDark = useSyncExternalStore(
    subscribeToColorScheme,
    getColorSchemeSnapshot,
    () => false,
  );

  const resolvedTheme: ThemeContextValue["resolvedTheme"] =
    theme === "system" ? (systemDark ? "dark" : "light") : theme;

  useEffect(() => {
    const root = document.documentElement;
    root.classList.remove("light", "dark");
    root.classList.add(resolvedTheme);
    root.style.colorScheme = resolvedTheme;
  }, [resolvedTheme]);

  const setTheme = useCallback(
    (nextTheme: Theme) => {
      localStorage.setItem(storageKey, nextTheme);
      setThemeState(nextTheme);
    },
    [storageKey],
  );

  const value = useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme, setTheme],
  );

  return <ThemeProviderContext value={value}>{children}</ThemeProviderContext>;
}

export const useTheme = () => {
  const context = use(ThemeProviderContext);

  if (!context) throw new Error("useTheme must be used within a ThemeProvider");

  return context;
};
