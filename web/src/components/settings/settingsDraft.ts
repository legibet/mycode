/**
 * Settings form state: built from the `GET /api/settings` config and
 * serialized back into the `PUT /api/settings` payload.
 */

import type {
  GlobalConfig,
  GlobalProviderEntry,
  PermissionLevel,
  PermissionMode,
  WebFetchProvider,
  WebSearchProvider,
} from "@/types";

export type WebKeyProvider = "tavily" | "exa";

/** An API key field. The server masks literal secrets: only `${VAR}` refs
 * come back as `api_key_input`, a literal key shows up as `api_key_saved`. */
export interface ApiKeyDraft {
  api_key_input: string;
  api_key_dirty: boolean;
  api_key_saved: boolean;
}

export interface ProviderDraft extends ApiKeyDraft {
  id: string;
  name: string;
  /** Original on-disk name; '' for entries the user just added. Drives the
   * "renamed → re-send api_key" rule in buildPayload. */
  original_name: string;
  type: string;
  models: string[];
  /** Per-model metadata overrides (context_window, etc) preserved opaquely so
   * loading + saving doesn't drop them. UI doesn't currently edit these. */
  model_overrides: Record<string, Record<string, unknown>>;
  base_url: string;
  /** Opt-in for generic openai_chat endpoints that accept reasoning_effort. */
  supports_reasoning_effort: boolean;
}

export interface SettingsDraft {
  compact_threshold: string; // form-friendly; '' means unset, 'disabled' for false
  permission_level: PermissionLevel;
  permission_mode: PermissionMode;
  web_fetch: WebFetchProvider;
  web_search: WebSearchProvider;
  web_keys: Record<WebKeyProvider, ApiKeyDraft>;
  providers: ProviderDraft[];
}

export function buildDraft(config: GlobalConfig): SettingsDraft {
  const ct = config.compact_threshold;
  const permission =
    typeof config.permission === "string" ? { level: config.permission } : config.permission;

  return {
    compact_threshold: ct === false ? "disabled" : typeof ct === "number" ? String(ct) : "",
    permission_level: permission?.level ?? "safe",
    permission_mode: permission?.mode ?? "ask",
    web_fetch: config.web?.fetch ?? "local",
    web_search: config.web?.search ?? "off",
    web_keys: {
      tavily: buildApiKeyDraft(config.web?.tavily),
      exa: buildApiKeyDraft(config.web?.exa),
    },
    providers: Object.entries(config.providers ?? {}).map(([name, entry], i) => ({
      id: `p-${i}-${name}`,
      name,
      original_name: name,
      type: entry.type ?? name,
      models: Array.isArray(entry.models) ? entry.models : Object.keys(entry.models ?? {}),
      model_overrides: entry.model_overrides ?? {},
      base_url: entry.base_url ?? "",
      supports_reasoning_effort: Boolean(entry.supports_reasoning_effort),
      ...buildApiKeyDraft(entry),
    })),
  };
}

function buildApiKeyDraft(
  entry: { api_key?: string | null; api_key_saved?: boolean } | undefined,
): ApiKeyDraft {
  return {
    api_key_input: typeof entry?.api_key === "string" ? entry.api_key : "",
    api_key_dirty: false,
    api_key_saved: Boolean(entry?.api_key_saved),
  };
}

export function buildPayload(draft: SettingsDraft): GlobalConfig {
  const config: GlobalConfig = {};

  if (draft.compact_threshold === "disabled") {
    config.compact_threshold = false;
  } else if (draft.compact_threshold.trim()) {
    const num = Number(draft.compact_threshold);
    if (Number.isFinite(num)) config.compact_threshold = num;
  }

  config.permission = {
    level: draft.permission_level,
    mode: draft.permission_mode,
  };

  const web: NonNullable<GlobalConfig["web"]> = {
    fetch: draft.web_fetch,
    search: draft.web_search,
  };
  for (const provider of ["tavily", "exa"] as const) {
    const key = draft.web_keys[provider];
    if (key.api_key_saved || key.api_key_input.trim()) {
      web[provider] = {
        api_key: key.api_key_dirty ? key.api_key_input.trim() : null,
      };
    }
  }
  config.web = web;

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
        entry.models = Object.fromEntries(p.models.map((id) => [id, overrides[id] ?? {}]));
      } else {
        entry.models = p.models;
      }
    }
    if (p.base_url.trim()) entry.base_url = p.base_url.trim();
    if (p.type === "openai_chat" && p.supports_reasoning_effort)
      entry.supports_reasoning_effort = true;
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
