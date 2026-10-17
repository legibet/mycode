import { describe, expect, it } from "vitest";

import { buildDraft, buildPayload } from "@/components/settings/settingsDraft";
import type { GlobalConfig } from "@/types";

describe("settings draft", () => {
  it("round-trips a loaded config, keeping saved keys and model overrides", () => {
    const loaded: GlobalConfig = {
      compact_threshold: false,
      permission: "standard",
      web: { fetch: "tavily", search: "off", tavily: { api_key: null, api_key_saved: true } },
      providers: {
        anthropic: { models: ["claude-sonnet-5"], api_key: null, api_key_saved: true },
        local: {
          type: "openai_chat",
          models: ["a", "b"],
          model_overrides: { a: { context_window: 32000 } },
          api_key: "${LOCAL_KEY}",
          api_key_saved: false,
        },
      },
    };

    expect(buildPayload(buildDraft(loaded))).toEqual({
      compact_threshold: false,
      permission: { level: "standard", mode: "ask" },
      web: { fetch: "tavily", search: "off", tavily: { api_key: null } },
      providers: {
        anthropic: { type: "anthropic", models: ["claude-sonnet-5"], api_key: null },
        local: {
          type: "openai_chat",
          models: { a: { context_window: 32000 }, b: {} },
          api_key: null,
        },
      },
    });
  });

  it("sends edited keys and re-sends the input when a provider is renamed", () => {
    const draft = buildDraft({
      providers: {
        cleared: { type: "anthropic", api_key: null, api_key_saved: true },
        renamed: { type: "anthropic", api_key: null, api_key_saved: true },
        moved: { type: "anthropic", api_key: "${KEY}" },
      },
    });
    const [cleared, renamed, moved] = draft.providers;
    if (!cleared || !renamed || !moved) throw new Error("providers missing");
    cleared.api_key_input = "";
    cleared.api_key_dirty = true;
    renamed.name = "renamed-2";
    moved.name = "moved-2";

    const providers = buildPayload(draft).providers ?? {};
    expect(providers["cleared"]?.api_key).toBe("");
    expect(providers["renamed-2"]?.api_key).toBe("");
    expect(providers["moved-2"]?.api_key).toBe("${KEY}");
  });
});
