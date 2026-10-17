import { describe, expect, it } from "vitest";

import type { RemoteConfig } from "@/api";
import type { LocalConfig } from "@/types";
import { getReasoningEffort, getReasoningEffortOverride } from "@/utils/config";

const remoteConfig: RemoteConfig = {
  providers: {
    openai: {
      name: "openai",
      models: ["gpt-5"],
      reasoning_efforts: { "gpt-5": ["low", "high"] },
      image_input_models: [],
      pdf_input_models: [],
    },
  },
};

function config(reasoningEfforts: LocalConfig["reasoningEfforts"]): LocalConfig {
  return {
    provider: "openai",
    model: "gpt-5",
    cwd: ".",
    reasoningEfforts,
  };
}

describe("reasoning effort config", () => {
  it("falls back to auto when a saved effort is no longer supported", () => {
    const current = config({ "openai/gpt-5": "medium" });

    expect(getReasoningEffortOverride(current, remoteConfig)).toBeUndefined();
    expect(getReasoningEffort(current, remoteConfig)).toBe("auto");
  });

  it("keeps a supported saved effort", () => {
    const current = config({ "openai/gpt-5": "high" });

    expect(getReasoningEffortOverride(current, remoteConfig)).toBe("high");
  });
});
