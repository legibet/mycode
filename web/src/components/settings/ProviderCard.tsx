/**
 * Single provider editor card. Self-contained input row group; the parent
 * holds the array of these in the panel state and merges into the PUT payload.
 */

import { Plus, X } from "lucide-react";
import { type KeyboardEvent, memo, type ReactNode, useState } from "react";

import { ApiKeyInput, envRefHint } from "@/components/settings/ApiKeyInput";
import { Field } from "@/components/settings/Field";
import type { ProviderDraft } from "@/components/settings/settingsDraft";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { cn } from "@/utils/cn";

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
    <div className="relative rounded-lg bg-card px-4 py-3.5 shadow-card">
      <Button
        variant="ghost-destructive"
        size="icon-sm"
        onClick={() => onRemove(draft.id)}
        aria-label={`Remove provider ${draft.name || "untitled"}`}
        className="absolute top-2 right-2"
      >
        <X className="size-3.5" />
      </Button>

      <div className="flex flex-col gap-2.5 pr-8">
        <Field label="Name" hint={renderNameHint(draft, usedTypesByOthers, duplicateName)}>
          {(id) => (
            <>
              <Input
                id={id}
                value={draft.name}
                onChange={(e) => update({ name: e.target.value })}
                placeholder="e.g. anthropic"
                spellCheck={false}
                autoCorrect="off"
                autoCapitalize="off"
                aria-invalid={duplicateName || !draft.name.trim()}
                className="font-mono"
              />
              {duplicateName && <span className="text-xs text-destructive/80">duplicate name</span>}
            </>
          )}
        </Field>

        <Field label="Type">
          {(id) => (
            <NativeSelect
              id={id}
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
          )}
        </Field>

        <Field label="Base URL">
          {(id) => (
            <Input
              id={id}
              value={draft.base_url}
              onChange={(e) => update({ base_url: e.target.value })}
              placeholder="(provider default)"
              spellCheck={false}
              autoCorrect="off"
              autoCapitalize="off"
              className="font-mono"
            />
          )}
        </Field>

        {/* The checkbox's own label carries the field name, so the row label names nothing. */}
        {draft.type === "openai_chat" && (
          <Field
            label="Reasoning effort"
            hint="Enable if this endpoint accepts a reasoning effort level."
            group
          >
            {() => (
              <label className="flex h-9 w-fit items-center gap-2.5 px-2 select-none">
                <Checkbox
                  checked={draft.supports_reasoning_effort}
                  onCheckedChange={(checked) => update({ supports_reasoning_effort: checked })}
                />
                <span className="text-ui text-foreground/90">
                  <span className="sr-only">Reasoning effort </span>Supported
                </span>
              </label>
            )}
          </Field>
        )}

        <Field label="API key" hint={apiKeyHint}>
          {(id) => (
            <ApiKeyInput
              id={id}
              draft={draft}
              onInput={(text) => update({ api_key_input: text, api_key_dirty: true })}
              clearLabel="Clear saved API key"
            />
          )}
        </Field>

        <Field
          label="Models"
          hint={
            draft.models.length === 0
              ? renderDefaultModelsHint(providerTypeDefaultModels[draft.type])
              : undefined
          }
          group
        >
          {(id) => (
            <div role="group" aria-labelledby={id} className="flex flex-col gap-2.5">
              {draft.models.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {draft.models.map((model) => (
                    <span
                      key={model}
                      className={cn(
                        "inline-flex h-7 items-center gap-1 rounded-sm pr-1 pl-2.5",
                        "bg-muted/60 font-mono text-xs text-foreground/90",
                      )}
                    >
                      {model}
                      <Button
                        variant="ghost-destructive"
                        size="icon-xs"
                        onClick={() => removeModel(model)}
                        aria-label={`Remove model ${model}`}
                      >
                        <X />
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
                    <Plus />
                  </Button>
                )}
              </div>
            </div>
          )}
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

  const refHint = envRefHint(draft.api_key_input, envByName);
  if (refHint) return refHint;

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
