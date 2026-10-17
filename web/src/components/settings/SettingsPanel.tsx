/**
 * Global settings panel.
 *
 * Reads/writes ~/.mycode/config.json via /api/settings. Project-level
 * .mycode/config.json files continue to override the global file at runtime
 * — the banner makes that explicit when the app is running inside a workspace
 * that has one.
 */

import { Laptop, Loader2, Moon, Plus, Sun, X } from "lucide-react";
import { type ReactNode, useCallback, useMemo, useState } from "react";

import { ApiKeyInput, envRefHint } from "@/components/settings/ApiKeyInput";
import { Field } from "@/components/settings/Field";
import { ProviderCard } from "@/components/settings/ProviderCard";
import {
  type ApiKeyDraft,
  buildDraft,
  buildPayload,
  type ProviderDraft,
  type SettingsDraft,
  type WebKeyProvider,
} from "@/components/settings/settingsDraft";
import { useTheme } from "@/components/ThemeProvider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { ResponsiveDialog } from "@/components/ui/responsive-dialog";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type {
  PermissionLevel,
  PermissionMode,
  SettingsResponse,
  Theme,
  WebFetchProvider,
  WebSearchProvider,
} from "@/types";
import { cn } from "@/utils/cn";
import { prettifyPath } from "@/utils/format";

interface SettingsPanelProps {
  open: boolean;
  onClose: () => void;
  settings: SettingsResponse | null;
  loadError?: string | undefined;
  /** Notified after a successful save so callers can update cached settings. */
  onSettingsSaved?: ((settings: SettingsResponse) => void) | undefined;
  /** Project-level config files in effect, used for the override-warning banner. */
  projectConfigPaths?: string[] | undefined;
}

const FALLBACK_CONFIG_PATH = "~/.mycode/config.json";

const PERMISSION_LEVEL_OPTIONS: { value: PermissionLevel; label: string }[] = [
  { value: "readonly", label: "readonly" },
  { value: "safe", label: "safe" },
  { value: "standard", label: "standard" },
  { value: "yolo", label: "yolo" },
];

const PERMISSION_MODE_OPTIONS: { value: PermissionMode; label: string }[] = [
  { value: "ask", label: "ask" },
  { value: "deny", label: "deny" },
];

const THEME_OPTIONS: { value: Theme; label: string; icon: ReactNode }[] = [
  {
    value: "system",
    label: "system",
    icon: <Laptop className="size-3.5" />,
  },
  { value: "light", label: "light", icon: <Sun className="size-3.5" /> },
  { value: "dark", label: "dark", icon: <Moon className="size-3.5" /> },
];

function webKeyHint(
  provider: WebKeyProvider,
  draft: ApiKeyDraft,
  envByName: Record<string, boolean>,
): ReactNode {
  const refHint = envRefHint(draft.api_key_input, envByName);
  if (refHint) return refHint;
  if (draft.api_key_saved && !draft.api_key_dirty) return "saved key";
  if (draft.api_key_input.trim()) return undefined;

  const envName = provider === "tavily" ? "TAVILY_API_KEY" : "EXA_API_KEY";
  if (envByName[envName]) {
    return (
      <>
        using env <code className="font-mono">{envName}</code>
      </>
    );
  }
  if (provider === "exa") {
    return <span className="text-destructive/80">key required</span>;
  }
  return "optional; requests use Tavily keyless access when unset";
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3.5">
      <h3 className="text-sm font-semibold text-foreground select-none">{title}</h3>
      <div className="flex flex-col gap-2.5">{children}</div>
    </section>
  );
}

export function SettingsPanel({
  open,
  onClose,
  settings,
  loadError,
  onSettingsSaved,
  projectConfigPaths,
}: SettingsPanelProps) {
  const { theme, setTheme } = useTheme();
  const [draft, setDraft] = useState<SettingsDraft>(() => buildDraft(settings?.config ?? {}));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const providerTypes = settings?.options.provider_types ?? [];
  const envByName = settings?.env ?? {};
  const providerTypeEnvVars = settings?.provider_type_env_vars ?? {};
  const providerTypeDefaultModels = settings?.provider_type_default_models ?? {};

  const usedTypesByOthersMap = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const p of draft.providers) {
      const others = new Set<string>();
      for (const other of draft.providers) {
        if (other.id !== p.id) others.add(other.type);
      }
      map.set(p.id, others);
    }
    return map;
  }, [draft.providers]);

  const duplicateNames = useMemo(() => {
    const seen = new Map<string, number>();
    for (const p of draft.providers) {
      const key = p.name.trim();
      if (!key) continue;
      seen.set(key, (seen.get(key) ?? 0) + 1);
    }
    const duplicates = new Set<string>();
    for (const [name, count] of seen) {
      if (count > 1) duplicates.add(name);
    }
    return duplicates;
  }, [draft.providers]);

  const hasInvalidProvider =
    duplicateNames.size > 0 || draft.providers.some((p) => !p.name.trim() || !p.type);
  const canSave = settings !== null && !saving && !hasInvalidProvider;

  const compactDisabled = draft.compact_threshold === "disabled";

  const updateProvider = useCallback((next: ProviderDraft) => {
    setDraft((prev) => ({
      ...prev,
      providers: prev.providers.map((p) => (p.id === next.id ? next : p)),
    }));
  }, []);

  const updateWebKey = (provider: WebKeyProvider, text: string) => {
    setDraft((prev) => ({
      ...prev,
      web_keys: {
        ...prev.web_keys,
        [provider]: { ...prev.web_keys[provider], api_key_input: text, api_key_dirty: true },
      },
    }));
  };

  const removeProvider = useCallback((id: string) => {
    setDraft((prev) => ({
      ...prev,
      providers: prev.providers.filter((p) => p.id !== id),
    }));
  }, []);

  const addProvider = () => {
    setDraft((prev) => {
      const type = providerTypes[0] ?? "";
      const taken = prev.providers.some((p) => p.type === type);
      const next: ProviderDraft = {
        id: `p-new-${Math.random().toString(36).slice(2, 8)}`,
        name: taken ? "" : type,
        original_name: "",
        type,
        models: [],
        model_overrides: {},
        base_url: "",
        supports_reasoning_effort: false,
        api_key_input: "",
        api_key_dirty: false,
        api_key_saved: false,
      };
      return { ...prev, providers: [...prev.providers, next] };
    });
  };

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: buildPayload(draft) }),
      });
      if (!res.ok) {
        let message = `Save failed (${res.status})`;
        try {
          const data = (await res.json()) as { detail?: string };
          if (data?.detail) message = data.detail;
        } catch {}
        throw new Error(message);
      }
      const updated = (await res.json()) as SettingsResponse;
      setDraft(buildDraft(updated.config));
      onSettingsSaved?.(updated);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  const projectOverrides = (projectConfigPaths ?? []).filter((path) => path !== settings?.path);

  const editsPath = `Edits ${prettifyPath(settings?.path ?? FALLBACK_CONFIG_PATH)}`;

  return (
    <ResponsiveDialog
      open={open}
      onClose={onClose}
      title="Settings"
      fullScreen
      className="max-h-[82vh] w-160"
    >
      {/* Header */}
      <div
        className={cn(
          "flex shrink-0 items-center justify-between",
          "border-b border-border/30",
          "h-12 px-4 md:h-14 md:px-6",
          "max-md:pt-[env(safe-area-inset-top)]",
          "max-md:h-[calc(3rem+env(safe-area-inset-top))]",
        )}
      >
        <h2 className="text-sm font-semibold text-foreground">Settings</h2>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          aria-label="Close"
          className="-mr-1 rounded-sm text-muted-foreground/70"
        >
          <X className="size-3.5" />
        </Button>
      </div>

      {/* Body */}
      <div className="scrollbar-subtle min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {!settings ? (
          <div className="flex h-48 items-center justify-center px-6 text-center text-xs text-muted-foreground/60">
            {loadError ? <span>{loadError}</span> : null}
          </div>
        ) : (
          <div className="flex flex-col gap-7 px-4 py-6 md:px-6">
            {projectOverrides.length > 0 && (
              <div className="rounded-md border border-border/40 bg-muted/20 px-3.5 py-2.5 text-xs leading-relaxed">
                <div className="text-foreground/80">Project-level config in effect:</div>
                <ul className="mt-1.5 ml-4 list-disc space-y-0.5 text-muted-foreground/80 marker:text-muted-foreground/40">
                  {projectOverrides.map((p) => (
                    <li key={p} className="truncate font-mono">
                      {prettifyPath(p)}
                    </li>
                  ))}
                </ul>
                <div className="mt-1.5 text-muted-foreground/70">
                  These continue to override settings saved here.
                </div>
              </div>
            )}

            <Section title="Appearance">
              <Field label="Theme">
                <ToggleGroup
                  aria-label="Theme"
                  value={[theme]}
                  onValueChange={([next]) => {
                    if (next) setTheme(next);
                  }}
                >
                  {THEME_OPTIONS.map((opt) => (
                    <ToggleGroupItem key={opt.value} value={opt.value}>
                      {opt.icon}
                      {opt.label}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
              </Field>
            </Section>

            <Section title="Context">
              <Field
                label="Compact"
                hint={
                  <>
                    {compactDisabled
                      ? "Auto-compaction disabled."
                      : "Trigger compaction at this fraction of context window (default 0.8)."}{" "}
                    <button
                      type="button"
                      onClick={() =>
                        setDraft((prev) => ({
                          ...prev,
                          compact_threshold: compactDisabled ? "" : "disabled",
                        }))
                      }
                      className="text-primary hover:underline focus-visible:underline focus-visible:outline-none"
                    >
                      {compactDisabled ? "Enable" : "Disable"}
                    </button>
                  </>
                }
              >
                <Input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={compactDisabled ? "" : draft.compact_threshold}
                  placeholder="0.8"
                  disabled={compactDisabled}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      compact_threshold: e.target.value,
                    }))
                  }
                  className="font-mono"
                />
              </Field>
            </Section>

            <Section title="Web access">
              <Field label="Fetch">
                <NativeSelect
                  aria-label="Web fetch provider"
                  value={draft.web_fetch}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      web_fetch: e.target.value as WebFetchProvider,
                    }))
                  }
                  className="font-mono"
                >
                  <NativeSelectOption value="local">local</NativeSelectOption>
                  <NativeSelectOption value="tavily">Tavily</NativeSelectOption>
                  <NativeSelectOption value="exa">Exa</NativeSelectOption>
                </NativeSelect>
              </Field>
              <Field label="Search">
                <NativeSelect
                  aria-label="Web search provider"
                  value={draft.web_search}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      web_search: e.target.value as WebSearchProvider,
                    }))
                  }
                  className="font-mono"
                >
                  <NativeSelectOption value="off">Off</NativeSelectOption>
                  <NativeSelectOption value="tavily">Tavily</NativeSelectOption>
                  <NativeSelectOption value="exa">Exa</NativeSelectOption>
                </NativeSelect>
              </Field>
              {(["tavily", "exa"] as const).map((provider) => {
                const selected = draft.web_fetch === provider || draft.web_search === provider;
                if (!selected) return null;
                const key = draft.web_keys[provider];
                const label = provider === "tavily" ? "Tavily" : "Exa";
                return (
                  <Field
                    key={provider}
                    label={`${label} key`}
                    hint={webKeyHint(provider, key, envByName)}
                  >
                    <ApiKeyInput
                      draft={key}
                      onInput={(text) => updateWebKey(provider, text)}
                      aria-label={`${label} API key`}
                      clearLabel={`Clear saved ${provider} API key`}
                    />
                  </Field>
                );
              })}
            </Section>

            <Section title="Permissions">
              <Field label="Level">
                {/* Mobile: 4 options compress to a select; desktop: segmented */}
                <div className="md:hidden">
                  <NativeSelect
                    value={draft.permission_level}
                    onChange={(e) =>
                      setDraft((prev) => ({
                        ...prev,
                        permission_level: e.target.value as PermissionLevel,
                      }))
                    }
                    className="font-mono"
                  >
                    {PERMISSION_LEVEL_OPTIONS.map((opt) => (
                      <NativeSelectOption key={opt.value} value={opt.value}>
                        {opt.label}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </div>
                <div className="max-md:hidden">
                  <ToggleGroup
                    aria-label="Permission level"
                    size="sm"
                    value={[draft.permission_level]}
                    onValueChange={([next]) => {
                      if (next) setDraft((prev) => ({ ...prev, permission_level: next }));
                    }}
                  >
                    {PERMISSION_LEVEL_OPTIONS.map((opt) => (
                      <ToggleGroupItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                </div>
              </Field>
              <Field label="Mode">
                <ToggleGroup
                  aria-label="Permission mode"
                  size="sm"
                  value={[draft.permission_mode]}
                  onValueChange={([next]) => {
                    if (next) setDraft((prev) => ({ ...prev, permission_mode: next }));
                  }}
                >
                  {PERMISSION_MODE_OPTIONS.map((opt) => (
                    <ToggleGroupItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
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
                    usedTypesByOthers={usedTypesByOthersMap.get(p.id) ?? new Set()}
                    duplicateName={duplicateNames.has(p.name.trim())}
                    onChange={updateProvider}
                    onRemove={removeProvider}
                  />
                ))}
                <Button
                  variant="ghost"
                  size="lg"
                  onClick={addProvider}
                  className="border-dashed border-border/50 text-ui font-normal text-muted-foreground/80 hover:border-border hover:bg-muted/30"
                >
                  <Plus className="size-4" />
                  Add provider
                </Button>
              </div>
            </Section>
          </div>
        )}
      </div>

      {/* Footer */}
      <div
        className={cn(
          "shrink-0 border-t border-border/30",
          "max-md:pb-[env(safe-area-inset-bottom)]",
        )}
      >
        {/* Mobile: error stacked above buttons; no path (would truncate uselessly) */}
        <div className="flex flex-col gap-2 px-4 py-3 md:hidden">
          {error && <div className="text-xs leading-relaxed text-destructive/90">{error}</div>}
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="lg"
              onClick={onClose}
              className="flex-1 text-ui font-normal"
            >
              Cancel
            </Button>
            <Button
              size="lg"
              onClick={() => void handleSave()}
              disabled={!canSave}
              className="flex-2 text-ui"
            >
              {saving && <Loader2 className="size-3.5 animate-spin" />}
              Save
            </Button>
          </div>
        </div>

        {/* Desktop: path on the left, buttons on the right */}
        <div className="flex items-center justify-between gap-3 px-6 py-3 max-md:hidden">
          <div className="min-w-0 flex-1 truncate text-xs text-muted-foreground/70">
            {error ? <span className="text-destructive/90">{error}</span> : editsPath}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button variant="ghost" onClick={onClose} className="px-3.5 text-ui font-normal">
              Cancel
            </Button>
            <Button onClick={() => void handleSave()} disabled={!canSave} className="px-4 text-ui">
              {saving && <Loader2 className="size-3.5 animate-spin" />}
              Save
            </Button>
          </div>
        </div>
      </div>
    </ResponsiveDialog>
  );
}
