/**
 * Global settings panel.
 *
 * Reads/writes ~/.mycode/config.json via /api/settings. Project-level
 * .mycode/config.json files continue to override the global file at runtime
 * — the banner makes that explicit when the app is running inside a workspace
 * that has one.
 */

import { Laptop, Loader2, Moon, Plus, Sun, X } from 'lucide-react'
import { useCallback, useEffect, useId, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import type {
  GlobalConfig,
  GlobalProviderEntry,
  PermissionLevel,
  PermissionMode,
  ReasoningEffort,
  SettingsResponse,
  Theme,
} from '../../types'
import { cn } from '../../utils/cn'
import { isReasoningEffort } from '../../utils/config'
import { useTheme } from '../ThemeProvider'
import { Field, NativeSelect, Section, Segmented, TextInput } from './controls'
import { ProviderCard, type ProviderDraft } from './ProviderCard'

interface SettingsPanelProps {
  open: boolean
  onClose: () => void
  /** Notified after a successful save so callers can revalidate /api/config. */
  onSaved?: (() => void) | undefined
  /** Project-level config files in effect, used for the override-warning banner. */
  projectConfigPaths?: string[] | undefined
  globalConfigPath?: string | undefined
}

interface DraftState {
  default_provider: string
  /** Carried opaquely from disk; the panel doesn't render it (provider runtime
   * uses the provider's first listed model anyway), but PUT replaces the file
   * wholesale so we must pass it through to avoid wiping it on save. */
  default_model: string
  default_reasoning_effort: ReasoningEffort | ''
  compact_threshold: string // form-friendly; '' means unset, 'disabled' for false
  permission_level: PermissionLevel
  permission_mode: PermissionMode
  providers: ProviderDraft[]
}

const INITIAL_DRAFT: DraftState = {
  default_provider: '',
  default_model: '',
  default_reasoning_effort: '',
  compact_threshold: '',
  permission_level: 'safe',
  permission_mode: 'ask',
  providers: [],
}

const THEME_OPTIONS: { value: Theme; label: string; icon: React.ReactNode }[] =
  [
    {
      value: 'system',
      label: 'system',
      icon: <Laptop className="h-3.5 w-3.5" />,
    },
    { value: 'light', label: 'light', icon: <Sun className="h-3.5 w-3.5" /> },
    { value: 'dark', label: 'dark', icon: <Moon className="h-3.5 w-3.5" /> },
  ]

function buildDraft(response: SettingsResponse): DraftState {
  const { config } = response
  const fallbackType = response.options.provider_types[0] ?? ''
  const draft: DraftState = { ...INITIAL_DRAFT }

  draft.default_provider = config.default?.provider ?? ''
  draft.default_model = config.default?.model ?? ''

  const effort = config.default?.reasoning_effort
  if (isReasoningEffort(effort)) draft.default_reasoning_effort = effort

  const ct = config.default?.compact_threshold
  if (ct === false) draft.compact_threshold = 'disabled'
  else if (typeof ct === 'number') draft.compact_threshold = String(ct)

  const perm = config.permission
  if (typeof perm === 'string') {
    draft.permission_level = perm
  } else if (perm) {
    if (perm.level) draft.permission_level = perm.level
    if (perm.mode) draft.permission_mode = perm.mode
  }

  draft.providers = Object.entries(config.providers ?? {}).map(
    ([name, entry], i) => ({
      id: `p-${i}-${name}`,
      name,
      original_name: name,
      type: entry.type ?? name ?? fallbackType,
      models: Array.isArray(entry.models)
        ? entry.models
        : Object.keys(entry.models ?? {}),
      model_overrides: entry.model_overrides ?? {},
      base_url: entry.base_url ?? '',
      reasoning_effort: isReasoningEffort(entry.reasoning_effort)
        ? entry.reasoning_effort
        : '',
      api_key_input: typeof entry.api_key === 'string' ? entry.api_key : '',
      api_key_dirty: false,
      api_key_saved: Boolean(entry.api_key_saved),
    }),
  )

  return draft
}

function buildPayload(draft: DraftState): GlobalConfig {
  const config: GlobalConfig = {}

  const defaultSection: NonNullable<GlobalConfig['default']> = {}
  // We persist whatever the user explicitly picked. When the dropdown's
  // "effective" fallback (first-available) showed without the user touching it,
  // draft.default_provider is still empty — leave it empty on disk so this
  // stays a soft default rather than hard-coding into the file.
  if (draft.default_provider.trim())
    defaultSection.provider = draft.default_provider.trim()
  if (draft.default_model.trim())
    defaultSection.model = draft.default_model.trim()
  if (draft.default_reasoning_effort) {
    defaultSection.reasoning_effort = draft.default_reasoning_effort
  }
  if (draft.compact_threshold === 'disabled') {
    defaultSection.compact_threshold = false
  } else if (draft.compact_threshold.trim()) {
    const num = Number(draft.compact_threshold)
    if (Number.isFinite(num)) defaultSection.compact_threshold = num
  }
  if (Object.keys(defaultSection).length) config.default = defaultSection

  config.permission = {
    level: draft.permission_level,
    mode: draft.permission_mode,
  }

  const providers: Record<string, GlobalProviderEntry> = {}
  for (const p of draft.providers) {
    const name = p.name.trim()
    if (!name) continue
    const entry: GlobalProviderEntry = {}
    if (p.type) entry.type = p.type
    if (p.models.length) {
      // Re-attach known per-model overrides so saving doesn't drop them. Sending
      // the dict form when any override survives; otherwise the simpler list.
      const overrides: Record<string, Record<string, unknown>> = {}
      for (const id of p.models) {
        const o = p.model_overrides[id]
        if (o && Object.keys(o).length) overrides[id] = o
      }
      if (Object.keys(overrides).length) {
        entry.models = Object.fromEntries(
          p.models.map((id) => [id, overrides[id] ?? {}]),
        )
      } else {
        entry.models = p.models
      }
    }
    if (p.base_url.trim()) entry.base_url = p.base_url.trim()
    if (p.reasoning_effort) entry.reasoning_effort = p.reasoning_effort
    // api_key three-state: dirty → string (incl. empty=clear); not dirty → null=keep.
    // Renaming severs the on-disk lookup the server uses to preserve secrets, so
    // we send the current input verbatim (env-refs survive; literal "saved" keys
    // become empty, which forces the user to re-enter — better than silent loss).
    const renamed = p.original_name !== '' && p.original_name !== name
    entry.api_key = p.api_key_dirty || renamed ? p.api_key_input.trim() : null
    providers[name] = entry
  }
  if (Object.keys(providers).length) config.providers = providers

  return config
}

export function SettingsPanel({
  open,
  onClose,
  onSaved,
  projectConfigPaths,
  globalConfigPath,
}: SettingsPanelProps) {
  const { theme, setTheme } = useTheme()
  const [response, setResponse] = useState<SettingsResponse | null>(null)
  const [draft, setDraft] = useState<DraftState>(INITIAL_DRAFT)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const compactDisabledId = useId()

  // ── data ────────────────────────────────────────────────────────────────
  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/settings')
      if (!res.ok) throw new Error(`Request failed (${res.status})`)
      const data = (await res.json()) as SettingsResponse
      setResponse(data)
      setDraft(buildDraft(data))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load settings')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    void reload()
  }, [open, reload])

  // ── escape closes ───────────────────────────────────────────────────────
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // ── derived ─────────────────────────────────────────────────────────────
  const providerTypes = response?.options.provider_types ?? []
  const effortOptions = response?.options.reasoning_efforts ?? []
  const envByName = response?.env ?? {}
  const providerTypeEnvVars = response?.provider_type_env_vars ?? {}
  const providerTypeDefaultModels = response?.provider_type_default_models ?? {}

  // For each card, set of types already in use by *other* cards. Drives the
  // auto-sync rule (changing Type updates Name when the new type is free) and
  // the "alias required" hint.
  const usedTypesByOthersMap = useMemo(() => {
    const map = new Map<string, Set<string>>()
    for (const p of draft.providers) {
      const others = new Set(
        draft.providers.filter((o) => o.id !== p.id).map((o) => o.type),
      )
      map.set(p.id, others)
    }
    return map
  }, [draft.providers])

  const duplicateNames = useMemo(() => {
    const seen = new Map<string, number>()
    for (const p of draft.providers) {
      const key = p.name.trim()
      if (!key) continue
      seen.set(key, (seen.get(key) ?? 0) + 1)
    }
    return new Set(
      [...seen.entries()]
        .filter(([, count]) => count > 1)
        .map(([name]) => name),
    )
  }, [draft.providers])

  const providerOptions = useMemo(
    () =>
      draft.providers
        .map((p) => p.name.trim())
        .filter((name) => name.length > 0),
    [draft.providers],
  )

  // Always resolve to a name that's actually in the providers list. This drops
  // the awkward "(auto)" empty option: when default_provider is empty or stale
  // (e.g. user just deleted that provider), fall back to the first available.
  const effectiveDefaultProvider = providerOptions.includes(
    draft.default_provider,
  )
    ? draft.default_provider
    : (providerOptions[0] ?? '')

  const hasInvalidProvider =
    duplicateNames.size > 0 ||
    draft.providers.some((p) => !p.name.trim() || !p.type)

  // ── handlers ────────────────────────────────────────────────────────────
  // Stable callbacks so memoized ProviderCard children don't re-render when
  // unrelated parts of the panel state change.
  const updateProvider = useCallback((next: ProviderDraft) => {
    setDraft((prev) => ({
      ...prev,
      providers: prev.providers.map((p) => (p.id === next.id ? next : p)),
    }))
  }, [])

  const removeProvider = useCallback((id: string) => {
    setDraft((prev) => ({
      ...prev,
      providers: prev.providers.filter((p) => p.id !== id),
    }))
  }, [])

  const addProvider = useCallback(() => {
    setDraft((prev) => {
      // Default new card to the first listed type. Auto-bind name = type when
      // that type is free; otherwise leave it blank so the user must alias.
      // (ProviderCard re-applies this rule when Type changes later.)
      const type = providerTypes[0] ?? ''
      const taken = prev.providers.some((p) => p.type === type)
      const next: ProviderDraft = {
        id: `p-new-${Math.random().toString(36).slice(2, 8)}`,
        name: taken ? '' : type,
        original_name: '',
        type,
        models: [],
        model_overrides: {},
        base_url: '',
        reasoning_effort: '',
        api_key_input: '',
        api_key_dirty: false,
        api_key_saved: false,
      }
      return { ...prev, providers: [...prev.providers, next] }
    })
  }, [providerTypes])

  const handleSave = async () => {
    if (hasInvalidProvider || saving) return
    setSaving(true)
    setError(null)
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config: buildPayload(draft) }),
      })
      if (!res.ok) {
        let message = `Save failed (${res.status})`
        try {
          const data = (await res.json()) as { detail?: string }
          if (data?.detail) message = data.detail
        } catch {}
        throw new Error(message)
      }
      const updated = (await res.json()) as SettingsResponse
      setResponse(updated)
      setDraft(buildDraft(updated))
      onSaved?.()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save settings')
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  // ── render ──────────────────────────────────────────────────────────────
  const projectOverrides = (projectConfigPaths ?? []).filter(
    (path) => path !== globalConfigPath && path !== response?.path,
  )

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-end sm:items-center sm:justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
    >
      <button
        type="button"
        aria-label="Close settings"
        tabIndex={-1}
        className="absolute inset-0 bg-black/50 backdrop-blur-[3px] animate-backdrop-in cursor-default"
        onClick={onClose}
      />

      <div
        className={cn(
          'relative flex flex-col overflow-hidden',
          'bg-background border border-border/40 shadow-2xl',
          'w-full rounded-t-2xl max-h-[88vh]',
          'animate-sheet-in',
          'sm:rounded-xl sm:w-[640px] sm:max-h-[82vh]',
          'sm:animate-dialog-in',
        )}
      >
        {/* Mobile drag handle */}
        <div className="sm:hidden flex justify-center pt-2.5 pb-1 shrink-0">
          <div className="w-10 h-[3px] rounded-full bg-border/60" />
        </div>

        {/* Header */}
        <div className="flex items-center justify-between px-5 sm:px-6 py-3.5 border-b border-border/30 shrink-0">
          <div className="flex items-baseline gap-3 min-w-0">
            <h2 className="text-[14px] font-medium text-foreground">
              Settings
            </h2>
            {response?.path && (
              <span className="hidden sm:inline truncate text-[11px] font-mono text-muted-foreground/55">
                {prettifyPath(response.path)}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className={cn(
              'h-7 w-7 inline-flex items-center justify-center rounded',
              'text-muted-foreground/50 hover:text-foreground hover:bg-muted/60 transition-colors',
              'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
            )}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto overscroll-contain scrollbar-subtle">
          {loading && !response ? (
            <div className="flex items-center justify-center h-48 text-muted-foreground/40">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : (
            <div className="px-5 sm:px-6 py-5 flex flex-col gap-7">
              {projectOverrides.length > 0 && (
                <div className="rounded border border-border/40 bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground leading-relaxed font-mono">
                  Project-level config in effect:
                  <ul className="mt-1 ml-3 list-disc marker:text-muted-foreground/40">
                    {projectOverrides.map((p) => (
                      <li key={p} className="truncate">
                        {prettifyPath(p)}
                      </li>
                    ))}
                  </ul>
                  <span className="block mt-1 text-muted-foreground/65">
                    These continue to override settings saved here.
                  </span>
                </div>
              )}

              <Section title="Appearance">
                <Field label="Theme">
                  <Segmented<Theme>
                    value={theme}
                    options={THEME_OPTIONS}
                    onChange={setTheme}
                    ariaLabel="Theme"
                  />
                </Field>
              </Section>

              <Section title="Defaults">
                {providerOptions.length === 0 ? (
                  <div className="rounded border border-dashed border-border/50 px-3 py-2 text-[12px] text-muted-foreground/70">
                    Add a provider below to configure defaults.
                  </div>
                ) : (
                  <Field
                    label="Provider"
                    hint="Default model is the provider's first listed entry — reorder the chips below to change it."
                  >
                    <NativeSelect
                      value={effectiveDefaultProvider}
                      onChange={(e) =>
                        setDraft({ ...draft, default_provider: e.target.value })
                      }
                    >
                      {providerOptions.map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                    </NativeSelect>
                  </Field>
                )}
                <Field label="Reasoning">
                  <NativeSelect
                    value={draft.default_reasoning_effort || 'auto'}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        default_reasoning_effort: e.target
                          .value as ReasoningEffort,
                      })
                    }
                  >
                    {effortOptions.map((effort) => (
                      <option key={effort} value={effort}>
                        {effort}
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                <Field
                  label="Compact"
                  hint={
                    draft.compact_threshold === 'disabled'
                      ? 'Auto-compaction disabled'
                      : 'Trigger compaction at this fraction of context window (default 0.8)'
                  }
                >
                  <div className="flex items-center gap-3">
                    <TextInput
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={
                        draft.compact_threshold === 'disabled'
                          ? ''
                          : draft.compact_threshold
                      }
                      placeholder="0.8"
                      disabled={draft.compact_threshold === 'disabled'}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          compact_threshold: e.target.value,
                        })
                      }
                      className="flex-1"
                    />
                    <label
                      htmlFor={compactDisabledId}
                      className="shrink-0 inline-flex items-center gap-1.5 text-[11px] text-muted-foreground select-none cursor-pointer"
                    >
                      <input
                        id={compactDisabledId}
                        type="checkbox"
                        checked={draft.compact_threshold === 'disabled'}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            compact_threshold: e.target.checked
                              ? 'disabled'
                              : '',
                          })
                        }
                        className="h-3 w-3 accent-accent"
                      />
                      disable
                    </label>
                  </div>
                </Field>
              </Section>

              <Section title="Permissions">
                <Field label="Level">
                  <Segmented<PermissionLevel>
                    value={draft.permission_level}
                    onChange={(value) =>
                      setDraft({ ...draft, permission_level: value })
                    }
                    options={[
                      { value: 'readonly', label: 'readonly' },
                      { value: 'safe', label: 'safe' },
                      { value: 'standard', label: 'standard' },
                      { value: 'yolo', label: 'yolo' },
                    ]}
                    ariaLabel="Permission level"
                    size="sm"
                  />
                </Field>
                <Field label="Mode">
                  <Segmented<PermissionMode>
                    value={draft.permission_mode}
                    onChange={(value) =>
                      setDraft({ ...draft, permission_mode: value })
                    }
                    options={[
                      { value: 'ask', label: 'ask' },
                      { value: 'deny', label: 'deny' },
                    ]}
                    ariaLabel="Permission mode"
                    size="sm"
                  />
                </Field>
              </Section>

              <Section title="Providers">
                <div className="flex flex-col gap-3">
                  {draft.providers.map((p) => (
                    <ProviderCard
                      key={p.id}
                      draft={p}
                      providerTypes={providerTypes}
                      envByName={envByName}
                      providerTypeEnvVars={providerTypeEnvVars}
                      providerTypeDefaultModels={providerTypeDefaultModels}
                      usedTypesByOthers={
                        usedTypesByOthersMap.get(p.id) ?? new Set()
                      }
                      duplicateName={duplicateNames.has(p.name.trim())}
                      onChange={updateProvider}
                      onRemove={removeProvider}
                    />
                  ))}
                  <button
                    type="button"
                    onClick={addProvider}
                    className={cn(
                      'inline-flex items-center justify-center gap-1.5 h-9 rounded-lg',
                      'border border-dashed border-border/50 text-muted-foreground/70',
                      'hover:border-border hover:text-foreground hover:bg-muted/30 transition-colors',
                      'text-[12px]',
                    )}
                  >
                    <Plus className="h-3.5 w-3.5" />
                    Add provider
                  </button>
                </div>
              </Section>
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          className={cn(
            'flex items-center justify-between gap-3 px-5 sm:px-6 py-3 border-t border-border/30 shrink-0',
            'max-md:pb-[max(0.75rem,env(safe-area-inset-bottom))]',
          )}
        >
          <div className="text-[11px] text-muted-foreground/70 font-mono truncate min-w-0 flex-1">
            {error ? (
              <span className="text-destructive/90">{error}</span>
            ) : (
              `Edits ${prettifyPath(response?.path ?? globalConfigPath ?? '~/.mycode/config.json')}`
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={onClose}
              className={cn(
                'h-8 px-3 rounded text-[12px] text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors',
                'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
              )}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving || hasInvalidProvider}
              className={cn(
                'inline-flex items-center gap-1.5 h-8 px-3.5 rounded text-[12px] font-medium',
                'border border-accent/30 bg-accent/10 text-accent',
                'hover:bg-accent/20 hover:border-accent/50 transition-colors',
                'disabled:opacity-40 disabled:pointer-events-none',
                'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent/60',
              )}
            >
              {saving && <Loader2 className="h-3 w-3 animate-spin" />}
              Save
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function prettifyPath(value: string): string {
  if (!value) return ''
  const home = value.match(/^(\/Users\/[^/]+|\/home\/[^/]+)(.*)$/)
  return home ? `~${home[2] ?? ''}` : value
}
