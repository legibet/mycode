/**
 * Single provider editor card. Self-contained input row group; the parent
 * holds the array of these in the panel state and merges into the PUT payload.
 */

import { Plus, X } from 'lucide-react'
import { type KeyboardEvent, memo, useId, useState } from 'react'
import type { ReasoningEffort } from '../../types'
import { cn } from '../../utils/cn'
import { Field, NativeSelect, TextInput } from './controls'

export interface ProviderDraft {
  id: string
  name: string
  /** Original on-disk name; '' for entries the user just added. Drives the
   * "renamed → re-send api_key" rule in buildPayload (see SettingsPanel). */
  original_name: string
  type: string
  models: string[]
  /** Per-model metadata overrides (context_window, etc) preserved opaquely so
   * loading + saving doesn't drop them. UI doesn't currently edit these. */
  model_overrides: Record<string, Record<string, unknown>>
  base_url: string
  reasoning_effort: ReasoningEffort | ''
  api_key_input: string
  api_key_dirty: boolean
  api_key_saved: boolean
}

interface ProviderCardProps {
  draft: ProviderDraft
  providerTypes: string[]
  envByName: Record<string, boolean>
  providerTypeEnvVars: Record<string, string[]>
  providerTypeDefaultModels: Record<string, string[]>
  /** Types currently in use by sibling cards. Drives auto-name sync + alias hint. */
  usedTypesByOthers: Set<string>
  duplicateName: boolean
  onChange: (next: ProviderDraft) => void
  onRemove: (id: string) => void
}

const ENV_REF_RE = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/

export const ProviderCard = memo(function ProviderCard({
  draft,
  providerTypes,
  envByName,
  providerTypeEnvVars,
  providerTypeDefaultModels,
  usedTypesByOthers,
  duplicateName,
  onChange,
  onRemove,
}: ProviderCardProps) {
  const [modelInput, setModelInput] = useState('')
  const inputId = useId()

  const update = (patch: Partial<ProviderDraft>) =>
    onChange({ ...draft, ...patch })

  // Type change re-applies the name=type rule when the user hasn't taken the
  // name field over (name is empty, or still equals the previous type). When
  // the new type is already used by a sibling, drop name back to empty so the
  // user is forced to pick an alias rather than carrying a stale value forward.
  const handleTypeChange = (newType: string) => {
    const wasAutoBound = draft.name === '' || draft.name === draft.type
    if (!wasAutoBound) {
      update({ type: newType })
      return
    }
    const newTypeTaken = usedTypesByOthers.has(newType)
    update({ type: newType, name: newTypeTaken ? '' : newType })
  }

  const addModel = () => {
    const trimmed = modelInput.trim()
    if (!trimmed) return
    if (draft.models.includes(trimmed)) {
      setModelInput('')
      return
    }
    update({ models: [...draft.models, trimmed] })
    setModelInput('')
  }

  const removeModel = (model: string) =>
    update({ models: draft.models.filter((m) => m !== model) })

  const handleModelKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      addModel()
    } else if (e.key === 'Backspace' && !modelInput && draft.models.length) {
      e.preventDefault()
      const last = draft.models[draft.models.length - 1]
      if (last) removeModel(last)
    }
  }

  const apiKeyHint = computeApiKeyHint(draft, envByName, providerTypeEnvVars)

  return (
    <div
      className={cn(
        'relative rounded-lg border border-border/40 bg-card/40 px-4 py-3.5',
        'transition-colors hover:border-border/60',
      )}
    >
      <button
        type="button"
        onClick={() => onRemove(draft.id)}
        aria-label={`Remove provider ${draft.name || 'untitled'}`}
        className={cn(
          'absolute right-2.5 top-2.5 h-6 w-6 inline-flex items-center justify-center rounded',
          'text-muted-foreground/40 hover:text-destructive hover:bg-destructive/10 transition-colors',
          'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-destructive/60',
        )}
      >
        <X className="h-3.5 w-3.5" />
      </button>

      <div className="flex flex-col gap-2.5">
        <Field
          label="Name"
          hint={renderNameHint(draft, usedTypesByOthers, duplicateName)}
        >
          <TextInput
            value={draft.name}
            onChange={(e) => update({ name: e.target.value })}
            placeholder="e.g. anthropic"
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            invalid={duplicateName || !draft.name.trim()}
          />
          {duplicateName && (
            <span className="text-[11px] text-destructive/80 font-mono">
              duplicate name
            </span>
          )}
        </Field>

        <Field label="Type">
          <NativeSelect
            value={draft.type}
            onChange={(e) => handleTypeChange(e.target.value)}
          >
            {providerTypes.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </NativeSelect>
        </Field>

        <Field label="Base URL">
          <TextInput
            value={draft.base_url}
            onChange={(e) => update({ base_url: e.target.value })}
            placeholder="(provider default)"
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
          />
        </Field>

        <Field label="API key" hint={apiKeyHint.message}>
          <div className="relative flex items-center">
            <TextInput
              id={inputId}
              value={draft.api_key_input}
              onChange={(e) =>
                update({ api_key_input: e.target.value, api_key_dirty: true })
              }
              placeholder={
                draft.api_key_saved && !draft.api_key_dirty
                  ? '•••••• saved · type to replace'
                  : // biome-ignore lint/suspicious/noTemplateCurlyInString: literal hint shown to the user
                    'paste a key, or use ${ENV_VAR}'
              }
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
              autoComplete="off"
              type="text"
            />
            {draft.api_key_saved && !draft.api_key_dirty && (
              <button
                type="button"
                onClick={() =>
                  update({ api_key_input: '', api_key_dirty: true })
                }
                className="ml-2 shrink-0 text-[11px] text-muted-foreground/60 hover:text-destructive transition-colors"
                aria-label="Clear saved API key"
              >
                clear
              </button>
            )}
          </div>
        </Field>

        <Field
          label="Models"
          stacked
          hint={
            draft.models.length === 0
              ? renderDefaultModelsHint(providerTypeDefaultModels[draft.type])
              : undefined
          }
        >
          <div className="flex flex-wrap gap-1.5">
            {draft.models.map((model) => (
              <span
                key={model}
                className={cn(
                  'group inline-flex items-center gap-1 h-6 pl-2 pr-1 rounded',
                  'bg-muted/50 text-foreground/85 text-[12px] font-mono',
                )}
              >
                {model}
                <button
                  type="button"
                  onClick={() => removeModel(model)}
                  className="inline-flex h-4 w-4 items-center justify-center rounded text-muted-foreground/50 hover:text-destructive transition-colors"
                  aria-label={`Remove model ${model}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            <span className="inline-flex items-center gap-1">
              <input
                value={modelInput}
                onChange={(e) => setModelInput(e.target.value)}
                onKeyDown={handleModelKeyDown}
                placeholder={
                  draft.models.length === 0 ? 'add a model id…' : 'add another…'
                }
                spellCheck={false}
                autoCorrect="off"
                autoCapitalize="off"
                className={cn(
                  'h-6 min-w-[140px] flex-1 bg-transparent px-1 text-[12px] font-mono',
                  'border-b border-transparent hover:border-border/60 focus:border-accent transition-colors',
                  'placeholder:text-muted-foreground/40 focus:outline-none',
                )}
              />
              {modelInput && (
                <button
                  type="button"
                  onClick={addModel}
                  className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground/60 hover:text-foreground hover:bg-muted/50 transition-colors"
                  aria-label="Add model"
                >
                  <Plus className="h-3 w-3" />
                </button>
              )}
            </span>
          </div>
        </Field>
      </div>
    </div>
  )
})

function computeApiKeyHint(
  draft: ProviderDraft,
  envByName: Record<string, boolean>,
  providerTypeEnvVars: Record<string, string[]>,
): { message: React.ReactNode } {
  const renamed =
    draft.original_name !== '' && draft.original_name !== draft.name.trim()
  if (renamed && draft.api_key_saved && !draft.api_key_dirty) {
    return {
      message: (
        <span className="text-destructive/80">
          renamed from <code>{draft.original_name}</code> — saved key won't
          carry over, paste it again
        </span>
      ),
    }
  }

  const refMatch = draft.api_key_input.trim().match(ENV_REF_RE)
  if (refMatch) {
    const envName = refMatch[1] as string
    const set = envByName[envName] ?? false
    return {
      message: set ? (
        <>
          env <code>{envName}</code> is set
        </>
      ) : (
        <span className="text-destructive/80">
          env <code>{envName}</code> is not set
        </span>
      ),
    }
  }

  if (!draft.api_key_input.trim() && !draft.api_key_saved) {
    const builtins = providerTypeEnvVars[draft.type] || []
    if (builtins.length === 0) return { message: undefined }
    const primary = builtins[0] as string
    const set = envByName[primary] ?? false
    return {
      message: set ? (
        <>
          using env <code>{primary}</code>
        </>
      ) : (
        <span className="text-destructive/80">
          env <code>{primary}</code> not set — paste a key or set the env
        </span>
      ),
    }
  }

  return { message: undefined }
}

function renderNameHint(
  draft: ProviderDraft,
  usedTypesByOthers: Set<string>,
  duplicateName: boolean,
): React.ReactNode {
  if (duplicateName) return undefined
  const trimmed = draft.name.trim()
  if (!trimmed) {
    if (usedTypesByOthers.has(draft.type)) {
      return (
        <>
          type <code>{draft.type}</code> already used — pick an alias name
        </>
      )
    }
    return undefined
  }
  if (trimmed === draft.type) {
    return (
      <>
        overrides built-in <code>{draft.type}</code>
      </>
    )
  }
  return undefined
}

function renderDefaultModelsHint(
  defaults: string[] | undefined,
): React.ReactNode {
  if (!defaults || defaults.length === 0) {
    return 'add at least one model id'
  }
  return (
    <>
      uses built-in:{' '}
      {defaults.map((m, i) => (
        <span key={m}>
          <code>{m}</code>
          {i < defaults.length - 1 ? ', ' : ''}
        </span>
      ))}
    </>
  )
}
