import type { LocalConfig, ReasoningEffort, RemoteConfig } from '../types'

function isReasoningEffort(
  value: string | null | undefined,
): value is ReasoningEffort {
  return (
    value === '' ||
    value === 'auto' ||
    value === 'none' ||
    value === 'low' ||
    value === 'medium' ||
    value === 'high' ||
    value === 'xhigh'
  )
}

export function getDefaultReasoningEffort(
  remoteConfig: RemoteConfig | null,
  providerName: string,
  model: string,
): ReasoningEffort | '' {
  const providerInfo = remoteConfig?.providers?.[providerName]
  if (!providerInfo?.supports_reasoning_effort) return ''

  const reasoningModels = providerInfo.reasoning_models || []
  if (!reasoningModels.includes(model)) return ''

  const effort =
    providerInfo.reasoning_effort || remoteConfig?.default_reasoning_effort

  return isReasoningEffort(effort) ? effort : ''
}

export function normalizeConfigWithRemoteDefaults(
  config: LocalConfig,
  remoteConfig: RemoteConfig,
): LocalConfig {
  const providers = remoteConfig?.providers || {}
  const providerChanged = !config.provider || !providers[config.provider]
  const provider = providerChanged
    ? remoteConfig?.default?.provider || ''
    : config.provider
  const providerInfo = providers[provider]
  const modelChanged = !providerInfo?.models?.includes(config.model)
  const model = modelChanged ? providerInfo?.models?.[0] || '' : config.model
  const reasoningEffort =
    providerChanged || modelChanged ? '' : config.reasoningEffort

  return {
    ...config,
    provider,
    model,
    reasoningEffort,
  }
}
