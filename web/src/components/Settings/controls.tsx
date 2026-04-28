/**
 * Small primitives shared across the settings panel.
 *
 * Visual rules (kept in sync with the rest of the app):
 *   • monospace where the value is structured (provider/model ids, env vars)
 *   • controls are borderless by default; show a hairline only on hover/focus
 *   • accent color is reserved for active state; everything else uses muted tokens
 */

import type { ReactNode, SelectHTMLAttributes } from 'react'
import { cn } from '../../utils/cn'

interface FieldProps {
  label: string
  hint?: ReactNode
  children: ReactNode
  /** Stack label above the control instead of side-by-side. */
  stacked?: boolean
}

export function Field({ label, hint, children, stacked }: FieldProps) {
  // Stacked uses the same label typography as inline so labels look uniform
  // across the panel — only the geometry differs (label-on-top vs side-by-side).
  if (stacked) {
    return (
      <div className="flex flex-col gap-1.5">
        <div className="text-[12px] text-muted-foreground/80">{label}</div>
        {children}
        {hint && (
          <div className="text-[11px] text-muted-foreground/60 leading-relaxed font-mono">
            {hint}
          </div>
        )}
      </div>
    )
  }
  return (
    <div className="grid grid-cols-[110px_1fr] items-start gap-3 min-h-[32px]">
      <div className="text-[12px] text-muted-foreground/80 pt-1.5">{label}</div>
      <div className="flex flex-col gap-1 min-w-0">
        {children}
        {hint && (
          <div className="text-[11px] text-muted-foreground/60 leading-relaxed font-mono">
            {hint}
          </div>
        )}
      </div>
    </div>
  )
}

interface SectionProps {
  title: string
  children: ReactNode
}

export function Section({ title, children }: SectionProps) {
  // Section heading is dark + medium weight in title-case so it doesn't compete
  // with the muted, lower-case entry labels below it. Hairline rule reinforces
  // the boundary without adding noise.
  return (
    <section className="flex flex-col">
      <h3 className="text-[13px] font-semibold text-foreground select-none pb-2 mb-3 border-b border-border/30">
        {title}
      </h3>
      <div className="flex flex-col gap-2.5">{children}</div>
    </section>
  )
}

interface SegmentedProps<T extends string> {
  value: T
  options: { value: T; label: string; icon?: ReactNode }[]
  onChange: (value: T) => void
  ariaLabel?: string
  size?: 'sm' | 'md'
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
  size = 'md',
}: SegmentedProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        'inline-flex items-center rounded-md border border-border/40 bg-muted/30 p-0.5',
        size === 'sm' && 'p-[2px]',
      )}
    >
      {options.map((opt) => {
        const active = opt.value === value
        return (
          // biome-ignore lint/a11y/useSemanticElements: visual button-group pattern; native radios don't fit the segmented look
          <button
            type="button"
            key={opt.value}
            role="radio"
            aria-checked={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              'inline-flex items-center justify-center gap-1.5 rounded font-medium transition-colors',
              size === 'sm' ? 'h-6 px-2.5 text-[11px]' : 'h-7 px-3 text-[12px]',
              active
                ? 'bg-background text-foreground shadow-[0_1px_0_rgba(0,0,0,0.04)]'
                : 'text-muted-foreground hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/60',
            )}
          >
            {opt.icon}
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}

interface TextInputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean
}

export function TextInput({ className, invalid, ...props }: TextInputProps) {
  return (
    <input
      {...props}
      className={cn(
        'h-8 w-full bg-transparent px-2 text-[13px] font-mono',
        'border-b border-border/40',
        'transition-colors',
        'placeholder:text-muted-foreground/40',
        'hover:border-border/80',
        'focus:border-accent focus:outline-none',
        invalid && 'border-destructive/60 focus:border-destructive',
        className,
      )}
    />
  )
}

export function NativeSelect({
  className,
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className="relative inline-flex w-full min-w-0">
      <select
        {...props}
        className={cn(
          'appearance-none w-full h-8 pl-2 pr-7 text-[13px] font-mono bg-transparent',
          'border-b border-border/40',
          'transition-colors',
          'hover:border-border/80',
          'focus:border-accent focus:outline-none',
          className,
        )}
      >
        {children}
      </select>
      <span
        aria-hidden="true"
        className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-muted-foreground/50"
      >
        ▾
      </span>
    </div>
  )
}
