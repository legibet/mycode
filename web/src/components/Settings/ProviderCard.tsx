/**
 * Single provider editor card. Self-contained input row group; the parent
 * holds the array of these in the panel state and merges into the PUT payload.
 */

import { Plus, X } from "lucide-react";
import { type KeyboardEvent, memo, type ReactNode, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { cn } from "../../utils/cn";

export interface ProviderDraft {
  id: string;
  name: string;
  /** Original on-disk name; '' for entries the user just added. Drives the
   * "renamed → re-send api_key" rule in buildPayload (see SettingsPanel). */
  original_name: string;
  type: string;
  models: string[];
  /** Per-model metadata overrides (context_window, etc) preserved opaquely so
   * loading + saving doesn't drop them. UI doesn't currently edit these. */
  model_overrides: Record<string, Record<string, unknown>>;
  base_url: string;
  /** Opt-in for generic openai_chat endpoints that accept reasoning_effort. */
  supports_reasoning_effort: boolean;
  api_key_input: string;
  api_key_dirty: boolean;
  api_key_saved: boolean;
}

interface ProviderCardProps {
  draft: ProviderDraft;
  providerTypes: string[];
  envByName: Record<string, boolean>;
  providerTypeEnvVars: Record<string, string[]>;
  providerTypeDefaultModels: Record<string, string[]>;
  /** Types currently in use by sibling cards. Drives auto-name sync + alias hint. */
  usedTypesByOthers: Set<string>;
  duplicateName: boolean;
  onChange: (next: ProviderDraft) => void;
  onRemove: (id: string) => void;
}

const ENV_REF_RE = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/;

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
  const [modelInput, setModelInput] = useState("");
  const inputId = useId();

  const update = (patch: Partial<ProviderDraft>) => onChange({ ...draft, ...patch });

  // Type change re-applies the name=type rule when the user hasn't taken the
  // name field over (name is empty, or still equals the previous type). When
  // the new type is already used by a sibling, drop name back to empty so the
  // user is forced to pick an alias rather than carrying a stale value forward.
  const handleTypeChange = (newType: string) => {
    const wasAutoBound = draft.name === "" || draft.name === draft.type;
    if (!wasAutoBound) {
      update({ type: newType });
      return;
    }
    const newTypeTaken = usedTypesByOthers.has(newType);
    update({ type: newType, name: newTypeTaken ? "" : newType });
  };

  const addModel = () => {
    const trimmed = modelInput.trim();
    if (!trimmed) return;
    if (draft.models.includes(trimmed)) {
      setModelInput("");
      return;
    }
    update({ models: [...draft.models, trimmed] });
    setModelInput("");
  };

  const removeModel = (model: string) =>
    update({ models: draft.models.filter((m) => m !== model) });

  const handleModelKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addModel();
    } else if (e.key === "Backspace" && !modelInput && draft.models.length) {
      e.preventDefault();
      const last = draft.models[draft.models.length - 1];
      if (last) removeModel(last);
    }
  };

  const apiKeyHint = computeApiKeyHint(draft, envByName, providerTypeEnvVars);

  return (
    <div
      className={cn(
        "relative rounded-lg bg-card px-4 py-3.5 shadow-card",
        "transition-colors hover:border-border/60",
      )}
    >
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={() => onRemove(draft.id)}
        aria-label={`Remove provider ${draft.name || "untitled"}`}
        className="absolute top-2 right-2 text-muted-foreground/50 hover:bg-destructive/10 hover:text-destructive"
      >
        <X className="size-3.5" />
      </Button>

      <div className="flex flex-col gap-2.5 pr-8">
        <Field label="Name" hint={renderNameHint(draft, usedTypesByOthers, duplicateName)}>
          <Input
            value={draft.name}
            onChange={(e) => update({ name: e.target.value })}
            placeholder="e.g. anthropic"
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            aria-invalid={duplicateName || !draft.name.trim()}
            className="font-mono"
          />
          {duplicateName && <span className="text-[12px] text-destructive/80">duplicate name</span>}
        </Field>

        <Field label="Type">
          <NativeSelect
            value={draft.type}
            onChange={(e) => handleTypeChange(e.target.value)}
            className="font-mono"
          >
            {providerTypes.map((t) => (
              <NativeSelectOption key={t} value={t}>
                {t}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>

        <Field label="Base URL">
          <Input
            value={draft.base_url}
            onChange={(e) => update({ base_url: e.target.value })}
            placeholder="(provider default)"
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            className="font-mono"
          />
        </Field>

        {draft.type === "openai_chat" && (
          <Field
            label="Reasoning effort"
            hint="Enable if this endpoint accepts a reasoning effort level."
          >
            <label className="flex h-9 w-fit items-center gap-2.5 px-2 select-none">
              <Checkbox
                checked={draft.supports_reasoning_effort}
                onCheckedChange={(checked) => update({ supports_reasoning_effort: checked })}
              />
              <span className="text-[13px] text-foreground/90">
                <span className="sr-only">Reasoning effort </span>Supported
              </span>
            </label>
          </Field>
        )}

        <Field label="API key" hint={apiKeyHint}>
          <div className="flex items-center gap-2">
            <Input
              id={inputId}
              value={draft.api_key_input}
              onChange={(e) => update({ api_key_input: e.target.value, api_key_dirty: true })}
              placeholder={
                draft.api_key_saved && !draft.api_key_dirty
                  ? "•••••• saved · type to replace"
                  : "paste a key, or use ${ENV_VAR}"
              }
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
              autoComplete="off"
              type="text"
              className="font-mono"
            />
            {draft.api_key_saved && !draft.api_key_dirty && (
              <Button
                variant="ghost"
                size="lg"
                onClick={() => update({ api_key_input: "", api_key_dirty: true })}
                className="px-2 text-[12px] font-normal text-muted-foreground/70 hover:text-destructive"
                aria-label="Clear saved API key"
              >
                clear
              </Button>
            )}
          </div>
        </Field>

        <Field
          label="Models"
          hint={
            draft.models.length === 0
              ? renderDefaultModelsHint(providerTypeDefaultModels[draft.type])
              : undefined
          }
        >
          <div className="flex flex-col gap-2.5">
            {draft.models.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {draft.models.map((model) => (
                  <span
                    key={model}
                    className={cn(
                      "group inline-flex h-7 items-center gap-1 rounded-sm pr-1 pl-2.5",
                      "bg-muted/60 font-mono text-[12px] text-foreground/90",
                    )}
                  >
                    {model}
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      onClick={() => removeModel(model)}
                      className="size-5 rounded-sm text-muted-foreground/60 hover:bg-transparent hover:text-destructive"
                      aria-label={`Remove model ${model}`}
                    >
                      <X className="size-3" />
                    </Button>
                  </span>
                ))}
              </div>
            )}
            <div className="flex items-center gap-2">
              <Input
                value={modelInput}
                onChange={(e) => setModelInput(e.target.value)}
                onKeyDown={handleModelKeyDown}
                aria-label="Add model"
                placeholder={draft.models.length === 0 ? "add a model id…" : "add another…"}
                spellCheck={false}
                autoCorrect="off"
                autoCapitalize="off"
                className="flex-1 font-mono"
              />
              {modelInput && (
                <Button variant="ghost" size="icon-lg" onClick={addModel} aria-label="Add model">
                  <Plus className="size-4" />
                </Button>
              )}
            </div>
          </div>
        </Field>
      </div>
    </div>
  );
});

function computeApiKeyHint(
  draft: ProviderDraft,
  envByName: Record<string, boolean>,
  providerTypeEnvVars: Record<string, string[]>,
): ReactNode {
  const renamed = draft.original_name !== "" && draft.original_name !== draft.name.trim();
  if (renamed && draft.api_key_saved && !draft.api_key_dirty) {
    return (
      <span className="text-destructive/80">
        renamed from <code className="font-mono">{draft.original_name}</code>: saved key won't carry
        over, paste it again
      </span>
    );
  }

  const refMatch = draft.api_key_input.trim().match(ENV_REF_RE);
  if (refMatch) {
    const envName = refMatch[1] as string;
    const set = envByName[envName] ?? false;
    return set ? (
      <>
        env <code className="font-mono">{envName}</code> is set
      </>
    ) : (
      <span className="text-destructive/80">
        env <code className="font-mono">{envName}</code> is not set
      </span>
    );
  }

  if (!draft.api_key_input.trim() && !draft.api_key_saved) {
    const builtins = providerTypeEnvVars[draft.type] || [];
    if (builtins.length === 0) {
      return (
        <span className="text-destructive/80">
          API key required. Paste a key or use an environment variable
        </span>
      );
    }
    const primary = builtins[0] as string;
    const set = envByName[primary] ?? false;
    return set ? (
      <>
        using env <code className="font-mono">{primary}</code>
      </>
    ) : (
      <span className="text-destructive/80">
        env <code className="font-mono">{primary}</code> not set. Paste a key or set the env
      </span>
    );
  }

  return undefined;
}

function renderNameHint(
  draft: ProviderDraft,
  usedTypesByOthers: Set<string>,
  duplicateName: boolean,
): ReactNode {
  if (duplicateName) return undefined;
  const trimmed = draft.name.trim();
  if (!trimmed) {
    if (usedTypesByOthers.has(draft.type)) {
      return (
        <>
          type <code className="font-mono">{draft.type}</code> already used. pick an alias name
        </>
      );
    }
    return undefined;
  }
  if (trimmed === draft.type) {
    return (
      <>
        overrides built-in <code className="font-mono">{draft.type}</code>
      </>
    );
  }
  return undefined;
}

function renderDefaultModelsHint(defaults: string[] | undefined): ReactNode {
  if (!defaults || defaults.length === 0) {
    return "add at least one model id";
  }
  return (
    <>
      uses built-in:{" "}
      {defaults.map((m, i) => (
        <span key={m}>
          <code className="font-mono">{m}</code>
          {i < defaults.length - 1 ? ", " : ""}
        </span>
      ))}
    </>
  );
}

interface FieldProps {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}

/** Labelled settings row, shared with SettingsPanel. */
export function Field({ label, hint, children }: FieldProps) {
  // Desktop: 110px label column on the left, control + hint stacked on the right.
  // Mobile: everything stacked top-to-bottom because there's no horizontal room
  // for a label column on a 375px screen.
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5",
        "md:grid md:grid-cols-[110px_1fr] md:items-start md:gap-x-3 md:gap-y-0",
      )}
    >
      <div className="text-[12px] text-muted-foreground/80 select-none md:pt-2">{label}</div>
      <div className="flex min-w-0 flex-col gap-1.5">
        {children}
        {hint && <div className="text-[12px] leading-relaxed text-muted-foreground/70">{hint}</div>}
      </div>
    </div>
  );
}
