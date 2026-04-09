package config

import (
	"fmt"
	"os"
	"slices"
	"sort"
	"strings"

	"github.com/legibet/mycode-go/internal/models"
	"github.com/legibet/mycode-go/internal/provider"
)

// ResolveProvider resolves one provider alias or built-in provider id.
func ResolveProvider(settings Settings, providerName, model, apiKey, apiBase, reasoningEffort string) (ResolvedProvider, error) {
	selected := strings.TrimSpace(providerName)
	if selected == "" {
		selected = strings.TrimSpace(settings.DefaultProvider)
	}
	if selected != "" {
		return resolveProviderRuntime(settings, selected, model, apiKey, apiBase, reasoningEffort)
	}

	available := availableProviderReferences(settings)
	if len(available) > 0 {
		return resolveProviderRuntime(settings, available[0], model, apiKey, apiBase, reasoningEffort)
	}

	envNames := []string{}
	seen := map[string]struct{}{}
	for _, spec := range provider.Specs() {
		if !spec.AutoDiscoverable {
			continue
		}
		for _, envName := range spec.EnvAPIKeyNames {
			if _, ok := seen[envName]; ok {
				continue
			}
			seen[envName] = struct{}{}
			envNames = append(envNames, envName)
		}
	}
	checked := "<api key env>"
	if len(envNames) > 0 {
		checked = strings.Join(envNames, ", ")
	}
	return ResolvedProvider{}, fmt.Errorf("no available providers found; set one of the supported API key env vars (%s) or configure a provider in ~/.mycode/config.json or <workspace>/.mycode/config.json", checked)
}

// AvailableProviders returns currently selectable providers in stable order.
func AvailableProviders(settings Settings) []ResolvedProvider {
	names := availableProviderReferences(settings)
	out := make([]ResolvedProvider, 0, len(names))
	for _, name := range names {
		resolved, err := resolveProviderRuntime(settings, name, "", "", "", "")
		if err == nil {
			out = append(out, resolved)
		}
	}
	return out
}

func availableProviderReferences(settings Settings) []string {
	names := []string{}
	seen := map[string]struct{}{}
	configuredTypesWithCredentials := map[string]struct{}{}
	add := func(name string) {
		name = strings.TrimSpace(name)
		if name == "" {
			return
		}
		if _, ok := seen[name]; ok {
			return
		}
		configured, hasConfig := settings.Providers[name]
		providerType := name
		if hasConfig {
			providerType = configured.Type
		}
		if _, ok := provider.LookupSpec(providerType); !ok {
			return
		}

		if hasConfig {
			if !providerHasAPIKey(configured) {
				return
			}
			configuredTypesWithCredentials[providerType] = struct{}{}
		} else if apiKeyFromEnv(providerType) == "" {
			return
		}

		seen[name] = struct{}{}
		names = append(names, name)
	}

	add(settings.DefaultProvider)
	for _, name := range settings.providerOrder {
		if providerHasAPIKey(settings.Providers[name]) {
			add(name)
		}
	}
	for _, spec := range provider.Specs() {
		if !spec.AutoDiscoverable {
			continue
		}
		if _, skip := configuredTypesWithCredentials[spec.ID]; skip {
			continue
		}
		if apiKeyFromEnv(spec.ID) == "" {
			continue
		}
		add(spec.ID)
	}
	return names
}

func resolveProviderRuntime(settings Settings, selectedName, model, apiKey, apiBase, reasoningEffort string) (ResolvedProvider, error) {
	configured, hasConfig := settings.Providers[selectedName]
	providerType := selectedName
	if hasConfig {
		providerType = configured.Type
	}
	spec, ok := provider.LookupSpec(providerType)
	if !ok {
		supported := []string{}
		for _, candidate := range provider.Specs() {
			supported = append(supported, candidate.ID)
		}
		sort.Strings(supported)
		return ResolvedProvider{}, fmt.Errorf("unsupported provider %q; supported: %s", providerType, strings.Join(supported, ", "))
	}

	resolvedModel := strings.TrimSpace(model)
	if resolvedModel == "" && hasConfig && len(configured.Models) > 0 {
		models := append([]string(nil), configured.ModelOrder...)
		if len(models) == 0 {
			for name := range configured.Models {
				models = append(models, name)
			}
			sort.Strings(models)
		}
		resolvedModel = models[0]
	}
	if resolvedModel == "" && selectedName == settings.DefaultProvider && strings.TrimSpace(settings.DefaultModel) != "" {
		resolvedModel = strings.TrimSpace(settings.DefaultModel)
	}
	if resolvedModel == "" {
		if len(spec.DefaultModels) == 0 {
			return ResolvedProvider{}, fmt.Errorf("provider %q does not define any default models", selectedName)
		}
		resolvedModel = spec.DefaultModels[0]
	}

	meta := resolveMetadata(providerType, resolvedModel, hasConfig, configured)
	supportsReasoning := meta != nil && meta.SupportsReasoning != nil && *meta.SupportsReasoning
	supportsImageInput := meta != nil && meta.SupportsImageInput != nil && *meta.SupportsImageInput
	supportsPDFInput := meta != nil && meta.SupportsPDFInput != nil && *meta.SupportsPDFInput

	configuredEffort := normalizeReasoningEffort(reasoningEffort)
	if configuredEffort == "" {
		if hasConfig && configured.ReasoningEffort != "" {
			configuredEffort = normalizeReasoningEffort(configured.ReasoningEffort)
		} else if settings.DefaultReasoningEffort != "" {
			configuredEffort = normalizeReasoningEffort(settings.DefaultReasoningEffort)
		}
	}
	if configuredEffort != "" {
		if !slices.Contains(validReasoningEfforts, configuredEffort) {
			return ResolvedProvider{}, fmt.Errorf("unsupported reasoning_effort %q; supported: %s", configuredEffort, strings.Join(validReasoningEfforts, ", "))
		}
		if meta == nil || !supportsReasoning || !spec.SupportsReasoningEffort {
			configuredEffort = ""
		}
	}

	resolvedAPIBase := strings.TrimSpace(apiBase)
	if resolvedAPIBase == "" && hasConfig {
		resolvedAPIBase = strings.TrimSpace(configured.BaseURL)
	}
	if resolvedAPIBase == "" {
		resolvedAPIBase = spec.DefaultBaseURL
	}

	resolvedAPIKey := strings.TrimSpace(apiKey)
	if resolvedAPIKey == "" && hasConfig {
		if configured.APIKeyEnvVar != "" {
			resolvedAPIKey = strings.TrimSpace(os.Getenv(configured.APIKeyEnvVar))
			if resolvedAPIKey == "" {
				return ResolvedProvider{}, fmt.Errorf("missing API key env var %q referenced by provider %q", configured.APIKeyEnvVar, selectedName)
			}
		} else if configured.APIKey != "" {
			resolvedAPIKey = configured.APIKey
		}
	}
	if resolvedAPIKey == "" {
		resolvedAPIKey = apiKeyFromEnv(providerType)
	}
	if resolvedAPIKey == "" {
		checked := strings.Join(spec.EnvAPIKeyNames, ", ")
		if checked == "" {
			checked = "<api key env>"
		}
		return ResolvedProvider{}, fmt.Errorf("provider %q is selected but no API key is available; checked: %s", selectedName, checked)
	}

	maxTokens, contextWindow := defaultMaxOutputTokens, defaultContextWindow
	if meta != nil {
		maxTokens = defaultInt(meta.MaxOutputTokens, defaultMaxOutputTokens)
		contextWindow = defaultInt(meta.ContextWindow, defaultContextWindow)
	}
	return ResolvedProvider{
		ProviderName:         selectedName,
		ProviderType:         providerType,
		Model:                resolvedModel,
		APIKey:               resolvedAPIKey,
		APIBase:              resolvedAPIBase,
		ReasoningEffort:      configuredEffort,
		MaxTokens:            maxTokens,
		ContextWindow:        contextWindow,
		SupportsReasoning:    supportsReasoning,
		SupportsImageInput:   supportsImageInput,
		SupportsPDFInput:     supportsPDFInput,
		SupportsEffortToggle: spec.SupportsReasoningEffort,
	}, nil
}

func resolveMetadata(providerType, model string, hasConfig bool, configured ProviderConfig) *models.Metadata {
	meta := lookupModelMetadata(providerType, model)
	if !hasConfig {
		return meta
	}
	override, ok := configured.Models[model]
	if !ok {
		return meta
	}
	if meta == nil {
		meta = &models.Metadata{
			Provider: providerType,
			Model:    model,
		}
	}
	if override.ContextWindow > 0 {
		meta.ContextWindow = override.ContextWindow
	}
	if override.MaxOutputTokens > 0 {
		meta.MaxOutputTokens = override.MaxOutputTokens
	}
	if override.SupportsReasoning != nil {
		meta.SupportsReasoning = override.SupportsReasoning
	}
	if override.SupportsImageInput != nil {
		meta.SupportsImageInput = override.SupportsImageInput
	}
	if override.SupportsPDFInput != nil {
		meta.SupportsPDFInput = override.SupportsPDFInput
	}
	return meta
}

func providerHasAPIKey(providerConfig ProviderConfig) bool {
	if providerConfig.APIKeyEnvVar != "" {
		return strings.TrimSpace(os.Getenv(providerConfig.APIKeyEnvVar)) != ""
	}
	if providerConfig.APIKey != "" {
		return true
	}
	return apiKeyFromEnv(providerConfig.Type) != ""
}
