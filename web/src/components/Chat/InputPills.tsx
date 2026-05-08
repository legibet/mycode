/**
 * Triggers shown inside the input box bottom row: model, effort.
 *
 * Each is a plain text button followed by a tiny chevron. Popovers open
 * upward and are portalled by Base UI so they escape the chat area's
 * overflow-hidden container.
 */

import { Check, ChevronDown } from 'lucide-react'
import { memo, useMemo, useState } from 'react'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover'
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

const TRIGGER_BTN =
  'inline-flex items-center gap-0.5 h-6 px-1.5 rounded text-[12px] leading-none ' +
  'text-muted-foreground hover:text-foreground hover:bg-muted/60 ' +
  'transition-colors focus-visible:outline-none focus-visible:bg-muted/60 ' +
  'disabled:opacity-40 disabled:cursor-not-allowed'

const POPOVER_CONTENT_CLASS =
  'p-0 gap-0 border border-border/50 shadow-md bg-popover text-popover-foreground'

// ─── model trigger ──────────────────────────────────────────────────────────

interface ModelTriggerProps {
  config: LocalConfig
  remoteConfig: RemoteConfig | null
  onUpdateConfig: (config: LocalConfig) => void
}

interface ModelItem {
  providerKey: string
  providerName: string
  model: string
}

export const ModelTrigger = memo(function ModelTrigger({
  config,
  remoteConfig,
  onUpdateConfig,
}: ModelTriggerProps) {
  const [open, setOpen] = useState(false)

  const groups = useMemo(() => {
    const providers = remoteConfig?.providers || {}
    const entries: Array<[string, ProviderInfo]> = Object.entries(
      providers,
    ).filter((e): e is [string, ProviderInfo] => e[1] !== undefined)
    return entries.map(([providerKey, info]) => ({
      providerKey,
      providerName: info.name,
      items: (info.models || []).map<ModelItem>((model) => ({
        providerKey,
        providerName: info.name,
        model,
      })),
    }))
  }, [remoteConfig])

  const label = config.model || 'no-model'

  const select = (item: ModelItem) => {
    onUpdateConfig({
      ...config,
      provider: item.providerKey,
      model: item.model,
      reasoningEffort: '',
    })
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
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
        }
      />
      <PopoverContent
        side="top"
        align="start"
        sideOffset={6}
        className={cn(
          POPOVER_CONTENT_CLASS,
          'w-[320px] max-w-[calc(100vw-1rem)]',
        )}
      >
        <Command className="max-h-[min(60vh,360px)] bg-transparent">
          <CommandInput placeholder="Filter models…" />
          <CommandList>
            <CommandEmpty>No matches</CommandEmpty>
            {groups.map((group) => (
              <CommandGroup
                key={group.providerKey}
                heading={group.providerName}
              >
                {group.items.map((item) => {
                  const active =
                    item.providerKey === config.provider &&
                    item.model === config.model
                  return (
                    <CommandItem
                      key={`${item.providerKey}:${item.model}`}
                      value={`${item.providerKey}:${item.model}`}
                      keywords={[item.providerName, item.model]}
                      onSelect={() => select(item)}
                    >
                      <span className="flex-1 truncate font-mono text-[13px]">
                        {item.model}
                      </span>
                      {active && (
                        <Check className="h-3 w-3 shrink-0 text-accent" />
                      )}
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
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

  if (!supportsEffort) return null

  const select = (value: ReasoningEffort) => {
    if (!isReasoningEffort(value)) return
    onUpdateConfig({ ...config, reasoningEffort: value })
    setOpen(false)
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <button
            type="button"
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
        }
      />
      <PopoverContent
        side="top"
        align="end"
        sideOffset={6}
        className={cn(POPOVER_CONTENT_CLASS, 'w-[140px] py-1')}
      >
        {options.map((opt) => {
          const active = opt === current
          return (
            <button
              key={opt}
              type="button"
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
              {active && <Check className="h-3 w-3 shrink-0 text-accent" />}
            </button>
          )
        })}
      </PopoverContent>
    </Popover>
  )
})
