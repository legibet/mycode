package provider

// Spec is the static metadata for one built-in provider type.
type Spec struct {
	ID                      string
	Label                   string
	DefaultBaseURL          string
	EnvAPIKeyNames          []string
	DefaultModels           []string
	SupportsReasoningEffort bool
	AutoDiscoverable        bool
}

var specs = []Spec{
	{
		ID:                      "anthropic",
		Label:                   "Anthropic Messages",
		DefaultBaseURL:          "https://api.anthropic.com",
		EnvAPIKeyNames:          []string{"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"},
		DefaultModels:           []string{"claude-sonnet-4-6", "claude-opus-4-6"},
		SupportsReasoningEffort: true,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "openai",
		Label:                   "OpenAI Responses",
		DefaultBaseURL:          "https://api.openai.com/v1",
		EnvAPIKeyNames:          []string{"OPENAI_API_KEY"},
		DefaultModels:           []string{"gpt-5.4", "gpt-5.4-mini"},
		SupportsReasoningEffort: true,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "google",
		Label:                   "Google Gemini",
		DefaultBaseURL:          "https://generativelanguage.googleapis.com",
		EnvAPIKeyNames:          []string{"GEMINI_API_KEY", "GOOGLE_API_KEY"},
		DefaultModels:           []string{"gemini-3.1-pro-preview", "gemini-3-flash-preview"},
		SupportsReasoningEffort: true,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "deepseek",
		Label:                   "DeepSeek Chat Completions",
		DefaultBaseURL:          "https://api.deepseek.com",
		EnvAPIKeyNames:          []string{"DEEPSEEK_API_KEY"},
		DefaultModels:           []string{"deepseek-chat", "deepseek-reasoner"},
		SupportsReasoningEffort: false,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "zai",
		Label:                   "Z.AI Chat Completions",
		DefaultBaseURL:          "https://api.z.ai/api/paas/v4",
		EnvAPIKeyNames:          []string{"ZAI_API_KEY"},
		DefaultModels:           []string{"glm-5.1", "glm-5-turbo"},
		SupportsReasoningEffort: false,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "moonshotai",
		Label:                   "Moonshot Anthropic-Compatible",
		DefaultBaseURL:          "https://api.moonshot.ai/anthropic",
		EnvAPIKeyNames:          []string{"MOONSHOT_API_KEY"},
		DefaultModels:           []string{"kimi-k2.5"},
		SupportsReasoningEffort: true,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "minimax",
		Label:                   "MiniMax Anthropic-Compatible",
		DefaultBaseURL:          "https://api.minimax.io/anthropic",
		EnvAPIKeyNames:          []string{"MINIMAX_API_KEY"},
		DefaultModels:           []string{"MiniMax-M2.7", "MiniMax-M2.7-highspeed"},
		SupportsReasoningEffort: true,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "openrouter",
		Label:                   "OpenRouter Chat Completions",
		DefaultBaseURL:          "https://openrouter.ai/api/v1",
		EnvAPIKeyNames:          []string{"OPENROUTER_API_KEY"},
		DefaultModels:           []string{"openrouter/auto"},
		SupportsReasoningEffort: true,
		AutoDiscoverable:        true,
	},
	{
		ID:                      "openai_chat",
		Label:                   "OpenAI Chat Completions",
		DefaultBaseURL:          "https://api.openai.com/v1",
		EnvAPIKeyNames:          []string{"OPENAI_API_KEY"},
		DefaultModels:           []string{"gpt-5.4", "gpt-5.4-mini"},
		SupportsReasoningEffort: false,
		AutoDiscoverable:        false,
	},
}

var specByID = func() map[string]Spec {
	byID := make(map[string]Spec, len(specs))
	for _, spec := range specs {
		byID[spec.ID] = spec
	}
	return byID
}()

// Specs returns all built-in provider specs.
func Specs() []Spec {
	out := make([]Spec, len(specs))
	copy(out, specs)
	return out
}

// LookupSpec returns one built-in provider spec.
func LookupSpec(id string) (Spec, bool) {
	spec, ok := specByID[id]
	return spec, ok
}
