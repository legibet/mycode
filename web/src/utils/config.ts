/**
 * Config normalization utilities and reasoning effort validation.
 */

import type { LocalConfig, ReasoningEffort, RemoteConfig } from "../types";

export function isReasoningEffort(value: unknown): value is ReasoningEffort {
  return (
    value === "auto" ||
    value === "none" ||
    value === "minimal" ||
    value === "low" ||
    value === "medium" ||
    value === "high" ||
    value === "xhigh" ||
    value === "max"
  );
}

export function getReasoningEffortOptions(
  remoteConfig: RemoteConfig | null,
  providerName: string,
  model: string,
): ReasoningEffort[] {
  const providerInfo = remoteConfig?.providers?.[providerName];
  if (!providerInfo?.supports_reasoning_effort) return [];

  const efforts = providerInfo.reasoning_efforts?.[model] || [];
  return efforts.length ? [...efforts.toReversed(), "auto"] : [];
}

export function reasoningEffortKey(provider: string, model: string): string {
  return `${provider}/${model}`;
}

export function getReasoningEffort(
  config: LocalConfig,
  remoteConfig?: RemoteConfig | null,
): ReasoningEffort {
  return getReasoningEffortOverride(config, remoteConfig) || "auto";
}

export function getReasoningEffortOverride(
  config: LocalConfig,
  remoteConfig?: RemoteConfig | null,
): ReasoningEffort | undefined {
  const saved =
    config.reasoningEfforts[reasoningEffortKey(config.provider, config.model)];
  if (!saved || !remoteConfig) return undefined;
  return getReasoningEffortOptions(
    remoteConfig,
    config.provider,
    config.model,
  ).includes(saved)
    ? saved
    : undefined;
}

export function normalizeConfigWithRemoteDefaults(
  config: LocalConfig,
  remoteConfig: RemoteConfig,
): LocalConfig {
  const providers = remoteConfig?.providers || {};
  const providerChanged = !config.provider || !providers[config.provider];
  const provider = providerChanged
    ? remoteConfig?.default?.provider || ""
    : config.provider;
  const providerInfo = providers[provider];
  const modelChanged = !providerInfo?.models?.includes(config.model);
  const model = modelChanged ? providerInfo?.models?.[0] || "" : config.model;
  return {
    ...config,
    provider,
    model,
  };
}
