export function getDefaultReasoningEffort(remoteConfig, providerName, model) {
  const providerInfo = remoteConfig?.providers?.[providerName]
  if (!providerInfo?.supports_reasoning_effort) return ''

  const reasoningModels = providerInfo.reasoning_models || []
  if (!reasoningModels.includes(model)) return ''

  return (
    providerInfo.reasoning_effort ||
    remoteConfig?.default_reasoning_effort ||
    ''
  )
}

export function normalizeConfigWithRemoteDefaults(config, remoteConfig) {
  const providers = remoteConfig?.providers || {}
  const provider =
    config.provider && providers[config.provider]
      ? config.provider
      : remoteConfig?.default?.provider || ''
  const providerInfo = providers[provider]
  const model = providerInfo?.models?.includes(config.model)
    ? config.model
    : providerInfo?.models?.[0] || ''
  const reasoningEffort =
    config.reasoningEffort ||
    getDefaultReasoningEffort(remoteConfig, provider, model)

  return {
    ...config,
    provider,
    model,
    reasoningEffort,
  }
}
