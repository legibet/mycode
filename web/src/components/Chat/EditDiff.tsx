import { PatchDiff } from '@pierre/diffs/react'
import type { CSSProperties } from 'react'
import { useTheme } from '../ThemeProvider'

interface EditDiffProps {
  patch: string
}

const DIFF_THEMES = {
  dark: 'dark-plus',
  light: 'light-plus',
} as const

const baseDiffStyle = {
  '--diffs-font-family': '"JetBrains Mono", ui-monospace, monospace',
  '--diffs-header-font-family': '"JetBrains Mono", ui-monospace, monospace',
  '--diffs-font-size': '13px',
  '--diffs-line-height': '1.5',
  '--diffs-light-bg': 'hsl(var(--code-background))',
  '--diffs-dark-bg': 'hsl(var(--code-background))',
} as CSSProperties

const diffCss = `
[data-code] {
  overflow-x: auto;
}

[data-line] {
  min-width: max-content;
}

[data-diff] {
  margin: 0;
}

[data-separator='simple'] {
  min-height: 6px;
  background:
    linear-gradient(
      to bottom,
      transparent,
      transparent calc(50% - 1px),
      color-mix(in srgb, currentColor 12%, transparent) calc(50% - 1px),
      color-mix(in srgb, currentColor 12%, transparent) calc(50% + 1px),
      transparent calc(50% + 1px),
      transparent
    ),
    var(--diffs-bg);
}
`

export default function EditDiff({ patch }: EditDiffProps) {
  const { resolvedTheme } = useTheme()

  return (
    <div className="rounded-md bg-code overflow-hidden">
      <PatchDiff
        patch={patch}
        style={{ ...baseDiffStyle, colorScheme: resolvedTheme }}
        disableWorkerPool
        options={{
          theme: DIFF_THEMES,
          themeType: resolvedTheme,
          diffStyle: 'unified',
          diffIndicators: 'classic',
          disableFileHeader: true,
          hunkSeparators: 'simple',
          overflow: 'scroll',
          unsafeCSS: diffCss,
        }}
      />
    </div>
  )
}
