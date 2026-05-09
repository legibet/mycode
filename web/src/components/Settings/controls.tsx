/**
 * Small primitives shared across the settings panel.
 *
 * Visual rules (kept in sync with the rest of the app):
 *   • borderless inputs — hairline only on bottom; deepens on hover/focus
 *   • mono only for structured values (ids, env vars); labels and hints stay sans
 *   • accent reserved for active state and the primary action
 *   • three font sizes (14 section / 13 control / 12 label·hint), three opacities (/100 /70 /50)
 */

import type { ReactNode, SelectHTMLAttributes } from "react";
import { cn } from "../../utils/cn";

interface FieldProps {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}

export function Field({ label, hint, children }: FieldProps) {
  // Desktop: 110px label column on the left, control + hint stacked on the right.
  // Mobile: everything stacked top-to-bottom because there's no horizontal room
  // for a label column on a 375px screen.
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5",
        "md:grid md:grid-cols-[110px_1fr] md:gap-x-3 md:gap-y-0 md:items-start",
      )}
    >
      <div className="text-[12px] text-muted-foreground/80 select-none md:pt-2">
        {label}
      </div>
      <div className="flex flex-col gap-1.5 min-w-0">
        {children}
        {hint && (
          <div className="text-[12px] text-muted-foreground/70 leading-relaxed">
            {hint}
          </div>
        )}
      </div>
    </div>
  );
}

interface SectionProps {
  title: string;
  children: ReactNode;
}

export function Section({ title, children }: SectionProps) {
  return (
    <section className="flex flex-col gap-3.5">
      <h3 className="text-[14px] font-semibold text-foreground select-none">
        {title}
      </h3>
      <div className="flex flex-col gap-2.5">{children}</div>
    </section>
  );
}

interface SegmentedProps<T extends string> {
  value: T;
  options: { value: T; label: string; icon?: ReactNode }[];
  onChange: (value: T) => void;
  ariaLabel?: string;
  size?: "sm" | "md";
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
  size = "md",
}: SegmentedProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="inline-flex items-center rounded-md border border-border/50 bg-muted/30 p-0.5"
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          // biome-ignore lint/a11y/useSemanticElements: visual button-group pattern; native radios don't fit the segmented look
          <button
            type="button"
            key={opt.value}
            role="radio"
            aria-checked={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              "inline-flex items-center justify-center gap-1.5 rounded font-medium transition-colors",
              size === "sm" ? "h-7 px-2.5 text-[12px]" : "h-7 px-3 text-[12px]",
              active
                ? "bg-background text-foreground"
                : "text-muted-foreground/80 hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/60",
            )}
          >
            {opt.icon}
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

interface TextInputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
}

export function TextInput({ className, invalid, ...props }: TextInputProps) {
  return (
    <input
      {...props}
      className={cn(
        "h-9 w-full bg-transparent px-2",
        "text-[13px] font-mono",
        "border-b border-border/60",
        "transition-colors",
        "placeholder:text-muted-foreground/50",
        "hover:border-border",
        "focus:border-accent focus:outline-none",
        "disabled:opacity-50 disabled:hover:border-border/60",
        invalid && "border-destructive/70 focus:border-destructive",
        className,
      )}
    />
  );
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
          "appearance-none w-full h-9 pl-2 pr-7",
          "text-[13px] font-mono bg-transparent",
          "border-b border-border/60",
          "transition-colors",
          "hover:border-border",
          "focus:border-accent focus:outline-none",
          className,
        )}
      >
        {children}
      </select>
      <span
        aria-hidden="true"
        className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-muted-foreground/60"
      >
        ▾
      </span>
    </div>
  );
}
