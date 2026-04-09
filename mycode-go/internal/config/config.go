package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
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

func candidateConfigPaths(cwd string) []string {
	return []string{
		filepath.Join(ResolveHome(), "config.json"),
		filepath.Join(FindWorkspaceRoot(cwd), ".mycode", "config.json"),
	}
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
	switch v := value.(type) {
	case bool:
		return &v
	default:
		return nil
	}
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

func loadConfig(path string) (loadedConfig, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return loadedConfig{}, false
	}
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return loadedConfig{}, false
	}

	loaded := loadedConfig{
		Raw:        raw,
		ModelOrder: map[string][]string{},
	}
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
