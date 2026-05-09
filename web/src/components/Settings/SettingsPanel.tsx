/**
 * Global settings panel.
 *
 * Reads/writes ~/.mycode/config.json via /api/settings. Project-level
 * .mycode/config.json files continue to override the global file at runtime
 * — the banner makes that explicit when the app is running inside a workspace
 * that has one.
 */

import { Laptop, Loader2, Moon, Plus, Sun, X } from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import type {
  GlobalConfig,
  GlobalProviderEntry,
  PermissionLevel,
  PermissionMode,
  ReasoningEffort,
  SettingsResponse,
  Theme,
} from "../../types";
import { cn } from "../../utils/cn";
import { isReasoningEffort } from "../../utils/config";
import { useTheme } from "../ThemeProvider";
import { Field, NativeSelect, Section, Segmented, TextInput } from "./controls";
import { ProviderCard, type ProviderDraft } from "./ProviderCard";

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

interface DraftState {
  default_provider: string;
  /** Carried opaquely from disk; the panel doesn't render it (provider runtime
   * uses the provider's first listed model anyway), but PUT replaces the file
   * wholesale so we must pass it through to avoid wiping it on save. */
  default_model: string;
  default_reasoning_effort: ReasoningEffort | "";
  compact_threshold: string; // form-friendly; '' means unset, 'disabled' for false
  permission_level: PermissionLevel;
  permission_mode: PermissionMode;
  providers: ProviderDraft[];
}

const INITIAL_DRAFT: DraftState = {
  default_provider: "",
  default_model: "",
  default_reasoning_effort: "",
  compact_threshold: "",
  permission_level: "safe",
  permission_mode: "ask",
  providers: [],
};

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

const CANCEL_BTN_CLASS = cn(
  "inline-flex items-center justify-center rounded-md text-[13px]",
  "text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors",
  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
);

const SAVE_BTN_CLASS = cn(
  "inline-flex items-center justify-center gap-1.5 rounded-md text-[13px] font-medium",
  "bg-accent text-accent-foreground hover:bg-accent/90 transition-colors",
  "disabled:opacity-40 disabled:pointer-events-none",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
);

const THEME_OPTIONS: { value: Theme; label: string; icon: React.ReactNode }[] =
  [
    {
      value: "system",
      label: "system",
      icon: <Laptop className="size-3.5" />,
    },
    { value: "light", label: "light", icon: <Sun className="size-3.5" /> },
    { value: "dark", label: "dark", icon: <Moon className="size-3.5" /> },
  ];

function buildDraft(response: SettingsResponse): DraftState {
  const { config } = response;
  const fallbackType = response.options.provider_types[0] ?? "";
  const draft: DraftState = { ...INITIAL_DRAFT };

  draft.default_provider = config.default?.provider ?? "";
  draft.default_model = config.default?.model ?? "";

  const effort = config.default?.reasoning_effort;
  if (isReasoningEffort(effort)) draft.default_reasoning_effort = effort;

  const ct = config.default?.compact_threshold;
  if (ct === false) draft.compact_threshold = "disabled";
  else if (typeof ct === "number") draft.compact_threshold = String(ct);

  const perm = config.permission;
  if (typeof perm === "string") {
    draft.permission_level = perm;
  } else if (perm) {
    if (perm.level) draft.permission_level = perm.level;
    if (perm.mode) draft.permission_mode = perm.mode;
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
      base_url: entry.base_url ?? "",
      reasoning_effort: isReasoningEffort(entry.reasoning_effort)
        ? entry.reasoning_effort
        : "",
      api_key_input: typeof entry.api_key === "string" ? entry.api_key : "",
      api_key_dirty: false,
      api_key_saved: Boolean(entry.api_key_saved),
    }),
  );

  return draft;
}

function buildPayload(draft: DraftState): GlobalConfig {
  const config: GlobalConfig = {};

  const defaultSection: NonNullable<GlobalConfig["default"]> = {};
  if (draft.default_provider.trim())
    defaultSection.provider = draft.default_provider.trim();
  if (draft.default_model.trim())
    defaultSection.model = draft.default_model.trim();
  if (draft.default_reasoning_effort) {
    defaultSection.reasoning_effort = draft.default_reasoning_effort;
  }
  if (draft.compact_threshold === "disabled") {
    defaultSection.compact_threshold = false;
  } else if (draft.compact_threshold.trim()) {
    const num = Number(draft.compact_threshold);
    if (Number.isFinite(num)) defaultSection.compact_threshold = num;
  }
  if (Object.keys(defaultSection).length) config.default = defaultSection;

  config.permission = {
    level: draft.permission_level,
    mode: draft.permission_mode,
  };

  const providers: Record<string, GlobalProviderEntry> = {};
  for (const p of draft.providers) {
    const name = p.name.trim();
    if (!name) continue;
    const entry: GlobalProviderEntry = {};
    if (p.type) entry.type = p.type;
    if (p.models.length) {
      const overrides: Record<string, Record<string, unknown>> = {};
      for (const id of p.models) {
        const o = p.model_overrides[id];
        if (o && Object.keys(o).length) overrides[id] = o;
      }
      if (Object.keys(overrides).length) {
        entry.models = Object.fromEntries(
          p.models.map((id) => [id, overrides[id] ?? {}]),
        );
      } else {
        entry.models = p.models;
      }
    }
    if (p.base_url.trim()) entry.base_url = p.base_url.trim();
    if (p.reasoning_effort) entry.reasoning_effort = p.reasoning_effort;
    // api_key three-state: dirty → string (incl. empty=clear); not dirty → null=keep.
    // Renaming severs the on-disk lookup the server uses to preserve secrets, so
    // we send the current input verbatim (env-refs survive; literal "saved" keys
    // become empty, which forces the user to re-enter — better than silent loss).
    const renamed = p.original_name !== "" && p.original_name !== name;
    entry.api_key = p.api_key_dirty || renamed ? p.api_key_input.trim() : null;
    providers[name] = entry;
  }
  if (Object.keys(providers).length) config.providers = providers;

  return config;
}

export function SettingsPanel({
  open,
  onClose,
  settings,
  loadError,
  onSettingsSaved,
  projectConfigPaths,
}: SettingsPanelProps) {
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const { theme, setTheme } = useTheme();
  const [draft, setDraft] = useState<DraftState>(INITIAL_DRAFT);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !settings) return;
    setDraft(buildDraft(settings));
    setError(null);
  }, [open, settings]);

  const providerTypes = settings?.options.provider_types ?? [];
  const effortOptions = settings?.options.reasoning_efforts ?? [];
  const envByName = settings?.env ?? {};
  const providerTypeEnvVars = settings?.provider_type_env_vars ?? {};
  const providerTypeDefaultModels =
    settings?.provider_type_default_models ?? {};

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

  const providerOptions = useMemo(() => {
    const options: string[] = [];
    for (const p of draft.providers) {
      const name = p.name.trim();
      if (name) options.push(name);
    }
    return options;
  }, [draft.providers]);

  const effectiveDefaultProvider = providerOptions.includes(
    draft.default_provider,
  )
    ? draft.default_provider
    : (providerOptions[0] ?? "");

  const hasInvalidProvider =
    duplicateNames.size > 0 ||
    draft.providers.some((p) => !p.name.trim() || !p.type);

  const compactDisabled = draft.compact_threshold === "disabled";

  const updateProvider = useCallback((next: ProviderDraft) => {
    setDraft((prev) => ({
      ...prev,
      providers: prev.providers.map((p) => (p.id === next.id ? next : p)),
    }));
  }, []);

  const removeProvider = useCallback((id: string) => {
    setDraft((prev) => ({
      ...prev,
      providers: prev.providers.filter((p) => p.id !== id),
    }));
  }, []);

  const addProvider = useCallback(() => {
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
        reasoning_effort: "",
        api_key_input: "",
        api_key_dirty: false,
        api_key_saved: false,
      };
      return { ...prev, providers: [...prev.providers, next] };
    });
  }, [providerTypes]);

  const handleSave = async () => {
    if (!settings || hasInvalidProvider || saving) return;
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
      setDraft(buildDraft(updated));
      onSettingsSaved?.(updated);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  const projectOverrides = (projectConfigPaths ?? []).filter(
    (path) => path !== settings?.path,
  );

  const editsPath = `Edits ${prettifyPath(settings?.path ?? FALLBACK_CONFIG_PATH)}`;

  const body: ReactNode = (
    <>
      {/* Header */}
      <div
        className={cn(
          "flex items-center justify-between shrink-0",
          "border-b border-border/30",
          "px-4 md:px-6 h-12 md:h-14",
          "max-md:pt-[env(safe-area-inset-top)]",
          "max-md:h-[calc(3rem+env(safe-area-inset-top))]",
        )}
      >
        <h2 className="text-[14px] font-semibold text-foreground">Settings</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className={cn(
            "inline-flex items-center justify-center rounded -mr-1",
            "size-8",
            "text-muted-foreground/70 hover:text-foreground hover:bg-muted/60 transition-colors",
            "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          )}
        >
          <X className="size-4" />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain scrollbar-subtle">
        {!settings ? (
          <div className="flex items-center justify-center h-48 px-6 text-center text-[12px] text-muted-foreground/60">
            {loadError ? <span>{loadError}</span> : null}
          </div>
        ) : (
          <div className="px-4 md:px-6 py-6 flex flex-col gap-7">
            {projectOverrides.length > 0 && (
              <div className="rounded-md border border-border/40 bg-muted/20 px-3.5 py-2.5 text-[12px] leading-relaxed">
                <div className="text-foreground/80">
                  Project-level config in effect:
                </div>
                <ul className="mt-1.5 ml-4 list-disc text-muted-foreground/80 marker:text-muted-foreground/40 space-y-0.5">
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
                <div className="rounded-md border border-dashed border-border/50 px-3.5 py-2.5 text-[12px] text-muted-foreground/70">
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
                      setDraft((prev) => ({
                        ...prev,
                        default_provider: e.target.value,
                      }))
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
                  value={draft.default_reasoning_effort || "auto"}
                  onChange={(e) =>
                    setDraft((prev) => ({
                      ...prev,
                      default_reasoning_effort: e.target
                        .value as ReasoningEffort,
                    }))
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
                  compactDisabled ? (
                    <>
                      Auto-compaction disabled.{" "}
                      <button
                        type="button"
                        onClick={() =>
                          setDraft((prev) => ({
                            ...prev,
                            compact_threshold: "",
                          }))
                        }
                        className="text-accent hover:underline focus-visible:outline-none focus-visible:underline"
                      >
                        Enable
                      </button>
                    </>
                  ) : (
                    <>
                      Trigger compaction at this fraction of context window
                      (default 0.8).{" "}
                      <button
                        type="button"
                        onClick={() =>
                          setDraft((prev) => ({
                            ...prev,
                            compact_threshold: "disabled",
                          }))
                        }
                        className="text-accent hover:underline focus-visible:outline-none focus-visible:underline"
                      >
                        Disable
                      </button>
                    </>
                  )
                }
              >
                <TextInput
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
                />
              </Field>
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
                  >
                    {PERMISSION_LEVEL_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </NativeSelect>
                </div>
                <div className="max-md:hidden">
                  <Segmented<PermissionLevel>
                    value={draft.permission_level}
                    onChange={(value) =>
                      setDraft((prev) => ({
                        ...prev,
                        permission_level: value,
                      }))
                    }
                    options={PERMISSION_LEVEL_OPTIONS}
                    ariaLabel="Permission level"
                    size="sm"
                  />
                </div>
              </Field>
              <Field label="Mode">
                <Segmented<PermissionMode>
                  value={draft.permission_mode}
                  onChange={(value) =>
                    setDraft((prev) => ({ ...prev, permission_mode: value }))
                  }
                  options={PERMISSION_MODE_OPTIONS}
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
                    "inline-flex items-center justify-center gap-1.5 rounded-lg h-9",
                    "border border-dashed border-border/50 text-muted-foreground/80",
                    "hover:border-border hover:text-foreground hover:bg-muted/30 transition-colors",
                    "text-[13px]",
                  )}
                >
                  <Plus className="size-4" />
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
          "shrink-0 border-t border-border/30",
          "max-md:pb-[env(safe-area-inset-bottom)]",
        )}
      >
        {/* Mobile: error stacked above buttons; no path (would truncate uselessly) */}
        <div className="md:hidden flex flex-col gap-2 px-4 py-3">
          {error && (
            <div className="text-[12px] text-destructive/90 leading-relaxed">
              {error}
            </div>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className={cn(CANCEL_BTN_CLASS, "flex-1 h-9")}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={!settings || saving || hasInvalidProvider}
              className={cn(SAVE_BTN_CLASS, "flex-2 h-9")}
            >
              {saving && <Loader2 className="size-3.5 animate-spin" />}
              Save
            </button>
          </div>
        </div>

        {/* Desktop: path on the left, buttons on the right */}
        <div className="max-md:hidden flex items-center justify-between gap-3 px-6 py-3">
          <div className="text-[12px] text-muted-foreground/70 truncate min-w-0 flex-1">
            {error ? (
              <span className="text-destructive/90">{error}</span>
            ) : (
              editsPath
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={onClose}
              className={cn(CANCEL_BTN_CLASS, "h-8 px-3.5")}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={!settings || saving || hasInvalidProvider}
              className={cn(SAVE_BTN_CLASS, "h-8 px-4")}
            >
              {saving && <Loader2 className="size-3.5 animate-spin" />}
              Save
            </button>
          </div>
        </div>
      </div>
    </>
  );

  const handleOpenChange = (next: boolean) => {
    if (!next) onClose();
  };

  if (isDesktop) {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent
          showCloseButton={false}
          className="flex flex-col gap-0 p-0 w-160 max-w-[calc(100vw-2rem)] max-h-[82vh] sm:max-w-[calc(100vw-2rem)]"
        >
          <DialogTitle className="sr-only">Settings</DialogTitle>
          {body}
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="flex flex-col gap-0 p-0 h-dvh rounded-none"
      >
        <SheetTitle className="sr-only">Settings</SheetTitle>
        {body}
      </SheetContent>
    </Sheet>
  );
}

function prettifyPath(value: string): string {
  if (!value) return "";
  const home = value.match(/^(\/Users\/[^/]+|\/home\/[^/]+)(.*)$/);
  return home ? `~${home[2] ?? ""}` : value;
}
