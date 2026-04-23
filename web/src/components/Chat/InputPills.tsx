/**
 * Triggers shown inside the input box bottom row: model, effort.
 *
 * Each is a plain text button followed by a tiny chevron. Popovers open
 * upward and are portalled to <body> so they escape the chat area's
 * overflow-hidden container.
 */

import { Check, ChevronDown, Search } from 'lucide-react'
import type { CSSProperties, RefObject } from 'react'
import {
  memo,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import type {
  LocalConfig,
  ProviderInfo,
  ReasoningEffort,
  RemoteConfig,
} from '../../types'
import { cn } from '../../utils/cn'
import {
  getDefaultReasoningEffort,
  isReasoningEffort,
} from '../../utils/config'

// ─── shared ─────────────────────────────────────────────────────────────────

const TRIGGER_BTN =
  'inline-flex items-center gap-0.5 h-6 px-1.5 rounded text-[12px] leading-none ' +
  'text-muted-foreground hover:text-foreground hover:bg-muted/60 ' +
  'transition-colors focus-visible:outline-none focus-visible:bg-muted/60 ' +
  'disabled:opacity-40 disabled:cursor-not-allowed'

const POPOVER_BASE =
  'fixed z-[100] overflow-hidden ' +
  'rounded-lg border border-border bg-popover text-popover-foreground shadow-xl ' +
  'animate-fade-in-up'

type Align = 'start' | 'end'

// Compute fixed popover position above an anchor, on the chosen horizontal side.
function useAnchoredPopover(
  open: boolean,
  anchorRef: RefObject<HTMLElement | null>,
  align: Align,
): CSSProperties | null {
  const [style, setStyle] = useState<CSSProperties | null>(null)

  // biome-ignore lint/correctness/useExhaustiveDependencies: trigger-only on open/align
  useLayoutEffect(() => {
    if (!open) {
      setStyle(null)
      return
    }
    const recalc = () => {
      const el = anchorRef.current
      if (!el) return
      const rect = el.getBoundingClientRect()
      const gap = 6
      const next: CSSProperties = {
        position: 'fixed',
        bottom: window.innerHeight - rect.top + gap,
        maxHeight: rect.top - 16,
      }
      if (align === 'start') {
        next.left = rect.left
      } else {
        next.right = window.innerWidth - rect.right
      }
      setStyle(next)
    }
    recalc()
    window.addEventListener('resize', recalc)
    window.addEventListener('scroll', recalc, true)
    return () => {
      window.removeEventListener('resize', recalc)
      window.removeEventListener('scroll', recalc, true)
    }
  }, [open, align])

  return style
}

// Close popover on outside-mousedown / Esc. Both anchor and popover refs are
// considered "inside" — clicks within either keep the popover open.
function useDismiss(
  open: boolean,
  anchorRef: RefObject<HTMLElement | null>,
  popoverRef: RefObject<HTMLElement | null>,
  onClose: () => void,
) {
  useEffect(() => {
    if (!open) return
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (anchorRef.current?.contains(target)) return
      if (popoverRef.current?.contains(target)) return
      onClose()
    }
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('mousedown', onMouseDown)
    window.addEventListener('keydown', onEsc)
    return () => {
      window.removeEventListener('mousedown', onMouseDown)
      window.removeEventListener('keydown', onEsc)
    }
  }, [open, anchorRef, popoverRef, onClose])
}

// ─── model trigger ──────────────────────────────────────────────────────────

interface ModelTriggerProps {
  config: LocalConfig
  remoteConfig: RemoteConfig | null
  onUpdateConfig: (config: LocalConfig) => void
}

interface ModelItem {
  providerKey: string
  providerName: string
  providerType: string
  model: string
}

export const ModelTrigger = memo(function ModelTrigger({
  config,
  remoteConfig,
  onUpdateConfig,
}: ModelTriggerProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [cursor, setCursor] = useState(0)
  const anchorRef = useRef<HTMLButtonElement | null>(null)
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  const allModels = useMemo<ModelItem[]>(() => {
    const providers = remoteConfig?.providers || {}
    const entries: Array<[string, ProviderInfo]> = Object.entries(
      providers,
    ).filter((e): e is [string, ProviderInfo] => e[1] !== undefined)
    const out: ModelItem[] = []
    for (const [providerKey, info] of entries) {
      for (const model of info.models || []) {
        out.push({
          providerKey,
          providerName: info.name,
          providerType: info.type,
          model,
        })
      }
    }
    return out
  }, [remoteConfig])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return allModels
    return allModels.filter(
      (m) =>
        m.model.toLowerCase().includes(q) ||
        m.providerName.toLowerCase().includes(q) ||
        m.providerKey.toLowerCase().includes(q),
    )
  }, [allModels, query])

  const grouped = useMemo(() => {
    const groups: Array<{
      providerKey: string
      providerName: string
      items: ModelItem[]
    }> = []
    const index = new Map<string, number>()
    for (const item of filtered) {
      let i = index.get(item.providerKey)
      if (i === undefined) {
        i = groups.length
        index.set(item.providerKey, i)
        groups.push({
          providerKey: item.providerKey,
          providerName: item.providerName,
          items: [],
        })
      }
      groups[i]?.items.push(item)
    }
    return groups
  }, [filtered])

  const popoverStyle = useAnchoredPopover(open, anchorRef, 'start')
  useDismiss(open, anchorRef, popoverRef, () => setOpen(false))

  // Reset + focus on open
  // biome-ignore lint/correctness/useExhaustiveDependencies: trigger-only on open
  useEffect(() => {
    if (!open) return
    setQuery('')
    const activeIdx = allModels.findIndex(
      (m) => m.providerKey === config.provider && m.model === config.model,
    )
    setCursor(Math.max(0, activeIdx))
    const t = window.setTimeout(() => inputRef.current?.focus(), 20)
    return () => window.clearTimeout(t)
  }, [open])

  // Clamp cursor when filter shrinks
  useEffect(() => {
    if (cursor >= filtered.length) {
      setCursor(Math.max(0, filtered.length - 1))
    }
  }, [cursor, filtered.length])

  // Scroll cursor into view
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-cursor="${cursor}"]`)
      ?.scrollIntoView({ block: 'nearest' })
  }, [cursor])

  const selectIndex = (i: number) => {
    const item = filtered[i]
    if (!item) return
    const providerChanged = item.providerKey !== config.provider
    onUpdateConfig({
      ...config,
      provider: item.providerKey,
      model: item.model,
      apiBase: providerChanged ? '' : config.apiBase,
      apiKey: providerChanged ? '' : config.apiKey,
      reasoningEffort: '',
    })
    setOpen(false)
  }

  const label = config.model || 'no-model'

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(TRIGGER_BTN, 'min-w-0 max-w-full')}
        title={label}
      >
        <span className="truncate font-mono">{label}</span>
        <ChevronDown
          className={cn(
            'h-3 w-3 shrink-0 opacity-60 transition-transform',
            open && 'rotate-180',
          )}
        />
      </button>

      {open &&
        popoverStyle &&
        createPortal(
          <div
            ref={popoverRef}
            style={popoverStyle}
            className={cn(POPOVER_BASE, 'w-[320px] max-w-[90vw] flex flex-col')}
            role="listbox"
            aria-label="Select model"
          >
            <div className="flex items-center gap-2 h-9 px-3 border-b border-border/50 shrink-0">
              <Search className="h-3 w-3 shrink-0 text-muted-foreground/50" />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    setCursor((c) =>
                      Math.min(c + 1, Math.max(0, filtered.length - 1)),
                    )
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    setCursor((c) => Math.max(0, c - 1))
                  } else if (e.key === 'Enter') {
                    e.preventDefault()
                    selectIndex(cursor)
                  }
                }}
                placeholder="Filter models…"
                spellCheck={false}
                autoComplete="off"
                className="flex-1 bg-transparent text-[13px] focus:outline-none placeholder:text-muted-foreground/40 caret-accent"
              />
            </div>

            <div
              ref={listRef}
              className="overflow-y-auto scrollbar-subtle py-1 min-h-0"
            >
              {grouped.length === 0 && (
                <div className="px-3 py-6 text-center text-xs text-muted-foreground/50">
                  No matches
                </div>
              )}
              {grouped.map((group) => (
                <div key={group.providerKey}>
                  <div className="px-3 pt-2 pb-1 text-[10px] font-mono uppercase tracking-wider text-muted-foreground/50">
                    {group.providerName}
                  </div>
                  {group.items.map((item) => {
                    const i = filtered.indexOf(item)
                    const selected = i === cursor
                    const active =
                      item.providerKey === config.provider &&
                      item.model === config.model
                    return (
                      <button
                        key={`${item.providerKey}:${item.model}`}
                        type="button"
                        role="option"
                        aria-selected={active}
                        data-cursor={i}
                        onClick={() => selectIndex(i)}
                        onMouseEnter={() => setCursor(i)}
                        className={cn(
                          'flex items-center gap-2 w-full px-3 py-1.5 text-left transition-colors',
                          selected
                            ? 'bg-muted text-foreground'
                            : 'text-foreground/80 hover:text-foreground',
                        )}
                      >
                        <span className="flex-1 truncate font-mono text-[13px]">
                          {item.model}
                        </span>
                        {active && (
                          <Check className="h-3 w-3 shrink-0 text-accent" />
                        )}
                      </button>
                    )
                  })}
                </div>
              ))}
            </div>
          </div>,
          document.body,
        )}
    </>
  )
})

// ─── effort trigger ─────────────────────────────────────────────────────────

interface EffortTriggerProps {
  config: LocalConfig
  remoteConfig: RemoteConfig | null
  onUpdateConfig: (config: LocalConfig) => void
}

export const EffortTrigger = memo(function EffortTrigger({
  config,
  remoteConfig,
  onUpdateConfig,
}: EffortTriggerProps) {
  const [open, setOpen] = useState(false)
  const anchorRef = useRef<HTMLButtonElement | null>(null)
  const popoverRef = useRef<HTMLDivElement | null>(null)

  const activeProviderInfo = remoteConfig?.providers?.[config.provider]
  const reasoningModels = activeProviderInfo?.reasoning_models || []
  const supportsEffort = Boolean(
    activeProviderInfo?.supports_reasoning_effort &&
      reasoningModels.includes(config.model),
  )
  const options = remoteConfig?.reasoning_effort_options || []

  const current =
    config.reasoningEffort ||
    getDefaultReasoningEffort(remoteConfig, config.provider, config.model) ||
    'auto'

  const popoverStyle = useAnchoredPopover(open, anchorRef, 'end')
  useDismiss(open, anchorRef, popoverRef, () => setOpen(false))

  if (!supportsEffort) return null

  const select = (value: ReasoningEffort) => {
    if (!isReasoningEffort(value)) return
    onUpdateConfig({ ...config, reasoningEffort: value })
    setOpen(false)
  }

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={TRIGGER_BTN}
        title={`Reasoning effort: ${current}`}
      >
        <span className="font-mono">{current}</span>
        <ChevronDown
          className={cn(
            'h-3 w-3 shrink-0 opacity-60 transition-transform',
            open && 'rotate-180',
          )}
        />
      </button>

      {open &&
        popoverStyle &&
        createPortal(
          <div
            ref={popoverRef}
            style={popoverStyle}
            className={cn(POPOVER_BASE, 'min-w-[140px]')}
            role="listbox"
            aria-label="Select reasoning effort"
          >
            <div className="py-1">
              {options.map((opt) => {
                const active = opt === current
                return (
                  <button
                    key={opt}
                    type="button"
                    role="option"
                    aria-selected={active}
                    onClick={() => select(opt)}
                    className={cn(
                      'flex items-center gap-2 w-full px-3 py-1.5 text-left transition-colors',
                      active
                        ? 'bg-muted text-foreground'
                        : 'text-foreground/80 hover:bg-muted/50 hover:text-foreground',
                    )}
                  >
                    <span className="flex-1 font-mono text-[13px]">
                      {opt || 'auto'}
                    </span>
                    {active && (
                      <Check className="h-3 w-3 shrink-0 text-accent" />
                    )}
                  </button>
                )
              })}
            </div>
          </div>,
          document.body,
        )}
    </>
  )
})
