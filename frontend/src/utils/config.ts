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
