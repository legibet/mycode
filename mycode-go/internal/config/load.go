package config

import (
	"fmt"
	"maps"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/legibet/mycode-go/internal/provider"
)

// Load returns merged config for one cwd.
func Load(cwd string) (Settings, error) {
	resolvedCWD := absPath(defaultString(cwd, mustGetwd()))
	workspaceRoot := FindWorkspaceRoot(resolvedCWD)

	settings := Settings{
		Providers:        map[string]ProviderConfig{},
		CompactThreshold: defaultCompactThreshold,
		Port:             defaultPort,
		CWD:              resolvedCWD,
		WorkspaceRoot:    workspaceRoot,
	}

	mergedProviders := map[string]map[string]any{}
	mergedModelOrder := map[string][]string{}
	providerOrder := []string{}
	seenProviders := map[string]struct{}{}

	for _, path := range candidateConfigPaths(resolvedCWD) {
		loaded, ok := loadConfig(path)
		if !ok {
			continue
		}
		raw := loaded.Raw
		settings.ConfigPaths = append(settings.ConfigPaths, path)

		if rawDefault, ok := raw["default"].(map[string]any); ok {
			if value, ok := rawDefault["provider"].(string); ok {
				settings.DefaultProvider = strings.TrimSpace(value)
			}
			if value, ok := rawDefault["model"].(string); ok {
				settings.DefaultModel = strings.TrimSpace(value)
			}
			if _, exists := rawDefault["reasoning_effort"]; exists {
				settings.DefaultReasoningEffort = normalizeReasoningEffort(rawDefault["reasoning_effort"])
			}
			if threshold, ok := parseCompactThreshold(rawDefault["compact_threshold"]); ok {
				settings.CompactThreshold = threshold
			}
		}

		rawProviders, _ := raw["providers"].(map[string]any)
		keys := loaded.ProviderOrder
		if len(keys) == 0 {
			for name := range rawProviders {
				keys = append(keys, name)
			}
			sort.Strings(keys)
		}
		for _, name := range keys {
			entry, ok := rawProviders[name].(map[string]any)
			if !ok {
				continue
			}
			if _, seen := seenProviders[name]; !seen {
				seenProviders[name] = struct{}{}
				providerOrder = append(providerOrder, name)
			}
			merged := maps.Clone(mergedProviders[name])
			if merged == nil {
				merged = map[string]any{}
			}
			if _, exists := entry["type"]; exists {
				merged["type"] = entry["type"]
			}
			if _, exists := entry["models"]; exists {
				merged["models"] = entry["models"]
				if order := loaded.ModelOrder[name]; len(order) > 0 {
					mergedModelOrder[name] = append([]string(nil), order...)
				} else {
					delete(mergedModelOrder, name)
				}
			}
			if _, exists := entry["api_key"]; exists {
				apiKey, apiKeyEnvVar := parseConfigAPIKey(entry["api_key"])
				merged["api_key"] = apiKey
				merged["api_key_env_var"] = apiKeyEnvVar
			}
			if _, exists := entry["base_url"]; exists {
				merged["base_url"] = entry["base_url"]
			}
			if _, exists := entry["reasoning_effort"]; exists {
				merged["reasoning_effort"] = entry["reasoning_effort"]
			}
			mergedProviders[name] = merged
		}
	}

	providers, err := buildProviders(mergedProviders, providerOrder, mergedModelOrder)
	if err != nil {
		return Settings{}, err
	}
	settings.Providers = providers
	settings.providerOrder = providerOrder

	if port := strings.TrimSpace(os.Getenv("PORT")); port != "" {
		parsed, err := strconv.Atoi(port)
		if err == nil && parsed > 0 {
			settings.Port = parsed
		}
	}

	return settings, nil
}

func buildProviders(rawProviders map[string]map[string]any, order []string, modelOrder map[string][]string) (map[string]ProviderConfig, error) {
	providers := map[string]ProviderConfig{}
	for _, name := range order {
		raw := rawProviders[name]
		rawType, hasExplicitType := raw["type"]
		providerType := strings.TrimSpace(asString(rawType))
		if hasExplicitType && providerType == "" {
			providerType = "anthropic"
		}
		if providerType == "" {
			if _, ok := provider.LookupSpec(name); ok {
				providerType = name
			} else {
				return nil, fmt.Errorf("provider %q must set 'type'", name)
			}
		}
		if _, ok := provider.LookupSpec(providerType); !ok {
			return nil, fmt.Errorf("unsupported provider type %q", providerType)
		}

		modelsMap, orderedModels := normalizeModels(raw["models"], modelOrder[name])
		if len(modelsMap) == 0 {
			spec, _ := provider.LookupSpec(providerType)
			modelsMap = make(map[string]ModelConfig, len(spec.DefaultModels))
			for _, model := range spec.DefaultModels {
				modelsMap[model] = ModelConfig{}
			}
			orderedModels = append([]string(nil), spec.DefaultModels...)
		}

		providers[name] = ProviderConfig{
			Name:            name,
			Type:            providerType,
			Models:          modelsMap,
			ModelOrder:      orderedModels,
			APIKey:          strings.TrimSpace(asString(raw["api_key"])),
			APIKeyEnvVar:    strings.TrimSpace(asString(raw["api_key_env_var"])),
			BaseURL:         strings.TrimSpace(asString(raw["base_url"])),
			ReasoningEffort: normalizeReasoningEffort(raw["reasoning_effort"]),
		}
	}
	return providers, nil
}

func normalizeModels(raw any, order []string) (map[string]ModelConfig, []string) {
	modelMap, _ := raw.(map[string]any)
	if len(modelMap) == 0 {
		return nil, nil
	}
	out := make(map[string]ModelConfig, len(modelMap))
	keys := make([]string, 0, len(modelMap))
	seen := map[string]struct{}{}
	for _, name := range order {
		if _, ok := modelMap[name]; ok {
			keys = append(keys, name)
			seen[name] = struct{}{}
		}
	}
	extra := make([]string, 0, len(modelMap))
	for name := range modelMap {
		if _, ok := seen[name]; ok {
			continue
		}
		extra = append(extra, name)
	}
	sort.Strings(extra)
	keys = append(keys, extra...)
	for _, name := range keys {
		rawConfig, _ := modelMap[name].(map[string]any)
		config := ModelConfig{}
		if rawConfig != nil {
			config.ContextWindow = asInt(rawConfig["context_window"])
			config.MaxOutputTokens = asInt(rawConfig["max_output_tokens"])
			config.SupportsReasoning = asBoolPtr(rawConfig["supports_reasoning"])
			config.SupportsImageInput = asBoolPtr(rawConfig["supports_image_input"])
			config.SupportsPDFInput = asBoolPtr(rawConfig["supports_pdf_input"])
		}
		out[name] = config
	}
	return out, keys
}
