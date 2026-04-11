package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/legibet/mycode-go/internal/models"
	"github.com/legibet/mycode-go/internal/provider"
)

const (
	defaultHome             = "~/.mycode"
	defaultPort             = 8000
	defaultContextWindow    = 128000
	defaultMaxOutputTokens  = 16384
	defaultCompactThreshold = 0.8
)

var validReasoningEfforts = []string{"none", "low", "medium", "high", "xhigh"}

var lookupModelMetadata = models.Lookup

// ModelConfig overrides bundled metadata for one model.
type ModelConfig struct {
	ContextWindow      int   `json:"context_window"`
	MaxOutputTokens    int   `json:"max_output_tokens"`
	SupportsReasoning  *bool `json:"supports_reasoning,omitempty"`
	SupportsImageInput *bool `json:"supports_image_input,omitempty"`
	SupportsPDFInput   *bool `json:"supports_pdf_input,omitempty"`
}

// ProviderConfig defines one configured provider alias.
type ProviderConfig struct {
	Name            string                 `json:"-"`
	Type            string                 `json:"type"`
	Models          map[string]ModelConfig `json:"models"`
	ModelOrder      []string               `json:"-"`
	APIKey          string                 `json:"-"`
	APIKeyEnvVar    string                 `json:"-"`
	BaseURL         string                 `json:"base_url"`
	ReasoningEffort string                 `json:"reasoning_effort"`
}

// Settings is the resolved config view for one workspace.
type Settings struct {
	Providers              map[string]ProviderConfig
	DefaultProvider        string
	DefaultModel           string
	DefaultReasoningEffort string
	CompactThreshold       float64
	Port                   int
	CWD                    string
	WorkspaceRoot          string
	ConfigPaths            []string

	providerOrder []string
}

// ResolvedProvider is the runnable provider config.
type ResolvedProvider struct {
	ProviderName         string
	ProviderType         string
	Model                string
	APIKey               string
	APIBase              string
	ReasoningEffort      string
	MaxTokens            int
	ContextWindow        int
	SupportsReasoning    bool
	SupportsImageInput   bool
	SupportsPDFInput     bool
	SupportsEffortToggle bool
}

type loadedConfig struct {
	Raw           map[string]any
	ProviderOrder []string
	ModelOrder    map[string][]string
}

type orderedObject struct {
	order  []string
	values map[string]json.RawMessage
}

// ResolveHome returns the mycode home directory.
func ResolveHome() string {
	raw := strings.TrimSpace(os.Getenv("MYCODE_HOME"))
	if raw == "" {
		raw = defaultHome
	}
	return absPath(raw)
}

// ResolveSessionsDir returns the persisted sessions directory.
func ResolveSessionsDir() string {
	return filepath.Join(ResolveHome(), "sessions")
}

// FindWorkspaceRoot returns the nearest git root or cwd.
func FindWorkspaceRoot(cwd string) string {
	current := absPath(defaultString(cwd, mustGetwd()))
	for {
		if _, err := os.Stat(filepath.Join(current, ".git")); err == nil {
			return current
		}
		parent := filepath.Dir(current)
		if parent == current {
			return current
		}
		current = parent
	}
}

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

	configPaths := []string{
		filepath.Join(ResolveHome(), "config.json"),
		filepath.Join(FindWorkspaceRoot(resolvedCWD), ".mycode", "config.json"),
	}
	for _, path := range configPaths {
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
		meta = &models.Metadata{Provider: providerType, Model: model}
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
		if _, ok := seen[name]; !ok {
			extra = append(extra, name)
		}
	}
	sort.Strings(extra)
	keys = append(keys, extra...)
	for _, name := range keys {
		rawConfig, _ := modelMap[name].(map[string]any)
		cfg := ModelConfig{}
		if rawConfig != nil {
			cfg.ContextWindow = asInt(rawConfig["context_window"])
			cfg.MaxOutputTokens = asInt(rawConfig["max_output_tokens"])
			cfg.SupportsReasoning = asBoolPtr(rawConfig["supports_reasoning"])
			cfg.SupportsImageInput = asBoolPtr(rawConfig["supports_image_input"])
			cfg.SupportsPDFInput = asBoolPtr(rawConfig["supports_pdf_input"])
		}
		out[name] = cfg
	}
	return out, keys
}

func parseCompactThreshold(value any) (float64, bool) {
	switch v := value.(type) {
	case nil:
		return 0, false
	case bool:
		if !v {
			return 0, true
		}
		return 0, false
	case float64:
		if v < 0 || v > 1 {
			return 0, false
		}
		return v, true
	case int:
		if v < 0 || v > 1 {
			return 0, false
		}
		return float64(v), true
	default:
		return 0, false
	}
}

func normalizeReasoningEffort(value any) string {
	text := strings.TrimSpace(strings.ToLower(asString(value)))
	switch text {
	case "", "auto", "default":
		return ""
	case "off", "disabled":
		return "none"
	default:
		return text
	}
}

func parseConfigAPIKey(value any) (literal string, envVar string) {
	text := strings.TrimSpace(asString(value))
	if text == "" {
		return "", ""
	}
	if strings.HasPrefix(text, "${") && strings.HasSuffix(text, "}") {
		return "", strings.TrimSuffix(strings.TrimPrefix(text, "${"), "}")
	}
	return text, ""
}

func apiKeyFromEnv(providerType string) string {
	spec, ok := provider.LookupSpec(providerType)
	if !ok {
		return ""
	}
	for _, name := range spec.EnvAPIKeyNames {
		if value := strings.TrimSpace(os.Getenv(name)); value != "" {
			return value
		}
	}
	return ""
}

func loadConfig(path string) (loadedConfig, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return loadedConfig{}, false
	}
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return loadedConfig{}, false
	}

	loaded := loadedConfig{Raw: raw, ModelOrder: map[string][]string{}}
	root, err := parseOrderedObject(data)
	if err != nil {
		return loaded, true
	}
	rawProviders, ok := root.values["providers"]
	if !ok {
		return loaded, true
	}
	providers, err := parseOrderedObject(rawProviders)
	if err != nil {
		return loaded, true
	}
	loaded.ProviderOrder = append([]string(nil), providers.order...)
	for _, name := range providers.order {
		rawProvider, ok := providers.values[name]
		if !ok {
			continue
		}
		providerObject, err := parseOrderedObject(rawProvider)
		if err != nil {
			continue
		}
		rawModels, ok := providerObject.values["models"]
		if !ok {
			continue
		}
		modelsObject, err := parseOrderedObject(rawModels)
		if err != nil {
			continue
		}
		loaded.ModelOrder[name] = append([]string(nil), modelsObject.order...)
	}
	return loaded, true
}

func parseOrderedObject(data []byte) (orderedObject, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	token, err := decoder.Token()
	if err != nil {
		return orderedObject{}, err
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '{' {
		return orderedObject{}, fmt.Errorf("expected object")
	}
	result := orderedObject{values: map[string]json.RawMessage{}}
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return orderedObject{}, err
		}
		key, ok := token.(string)
		if !ok {
			return orderedObject{}, fmt.Errorf("expected object key")
		}
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err != nil {
			return orderedObject{}, err
		}
		result.order = append(result.order, key)
		result.values[key] = raw
	}
	if _, err := decoder.Token(); err != nil {
		return orderedObject{}, err
	}
	return result, nil
}

func defaultInt(value, fallback int) int {
	if value > 0 {
		return value
	}
	return fallback
}

func absPath(path string) string {
	if path == "" {
		return ""
	}
	if strings.HasPrefix(path, "~/") {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, strings.TrimPrefix(path, "~/"))
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return filepath.Clean(path)
	}
	return filepath.Clean(absolute)
}

func asString(value any) string {
	text, _ := value.(string)
	return text
}

func asInt(value any) int {
	switch v := value.(type) {
	case float64:
		return int(v)
	case int:
		return v
	case json.Number:
		n, _ := v.Int64()
		return int(n)
	default:
		return 0
	}
}

func asBoolPtr(value any) *bool {
	if v, ok := value.(bool); ok {
		return &v
	}
	return nil
}

func defaultString(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

func mustGetwd() string {
	wd, err := os.Getwd()
	if err != nil {
		panic(err)
	}
	return wd
}
