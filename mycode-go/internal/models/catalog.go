package models

import (
	_ "embed"
	"encoding/json"
	"strings"
	"sync"
)

//go:embed models_catalog.json
var catalogJSON []byte

// Metadata is the normalized model capability record.
type Metadata struct {
	Provider           string `json:"provider"`
	Model              string `json:"model"`
	ContextWindow      int    `json:"context_window"`
	MaxOutputTokens    int    `json:"max_output_tokens"`
	SupportsReasoning  *bool  `json:"supports_reasoning,omitempty"`
	SupportsImageInput *bool  `json:"supports_image_input,omitempty"`
	SupportsPDFInput   *bool  `json:"supports_pdf_input,omitempty"`
}

var (
	loadOnce sync.Once
	catalog  map[string]map[string]rawMetadata
)

type rawMetadata struct {
	ContextWindow      int   `json:"context_window"`
	MaxOutputTokens    int   `json:"max_output_tokens"`
	SupportsReasoning  *bool `json:"supports_reasoning"`
	SupportsImageInput *bool `json:"supports_image_input"`
	SupportsPDFInput   *bool `json:"supports_pdf_input"`
}

// Lookup returns metadata for one provider/model pair.
func Lookup(providerType, model string) *Metadata {
	model = strings.TrimSpace(model)
	if model == "" {
		return nil
	}
	loadCatalog()
	if len(catalog) == 0 {
		return nil
	}

	if metadata := lookupEntry(providerType, model); metadata != nil {
		return metadata
	}

	// Strip optional "provider/" prefix (e.g. "openai/gpt-4o" → "gpt-4o").
	normalized := model
	if _, after, ok := strings.Cut(model, "/"); ok {
		normalized = strings.TrimSpace(after)
	}
	if fallback := defaultProvider(normalized); fallback != "" && fallback != providerType {
		if metadata := lookupEntry(fallback, normalized); metadata != nil {
			return metadata
		}
	}

	return lookupEntry("aihubmix", normalized)
}

func loadCatalog() {
	loadOnce.Do(func() {
		var parsed map[string]map[string]rawMetadata
		if err := json.Unmarshal(catalogJSON, &parsed); err != nil {
			catalog = map[string]map[string]rawMetadata{}
			return
		}
		catalog = parsed
	})
}

func lookupEntry(providerType, model string) *Metadata {
	provider := catalog[providerType]
	if len(provider) == 0 {
		return nil
	}
	entry, ok := provider[model]
	if !ok {
		return nil
	}
	return &Metadata{
		Provider:           providerType,
		Model:              model,
		ContextWindow:      entry.ContextWindow,
		MaxOutputTokens:    entry.MaxOutputTokens,
		SupportsReasoning:  entry.SupportsReasoning,
		SupportsImageInput: entry.SupportsImageInput,
		SupportsPDFInput:   entry.SupportsPDFInput,
	}
}

func defaultProvider(model string) string {
	switch {
	case strings.HasPrefix(strings.ToLower(model), "claude-"):
		return "anthropic"
	case strings.HasPrefix(strings.ToLower(model), "deepseek-"):
		return "deepseek"
	case strings.HasPrefix(strings.ToLower(model), "gemini-"):
		return "google"
	case strings.HasPrefix(strings.ToLower(model), "glm-"):
		return "zai"
	case strings.HasPrefix(strings.ToLower(model), "kimi-"):
		return "moonshotai"
	case strings.HasPrefix(strings.ToLower(model), "minimax-"):
		return "minimax"
	case strings.HasPrefix(strings.ToLower(model), "gpt-"),
		strings.HasPrefix(strings.ToLower(model), "o1"),
		strings.HasPrefix(strings.ToLower(model), "o3"),
		strings.HasPrefix(strings.ToLower(model), "o4"):
		return "openai"
	default:
		return ""
	}
}
