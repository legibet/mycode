package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/legibet/mycode-go/internal/models"
)

func TestLoadMergesGlobalAndProjectConfigs(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	project := filepath.Join(root, "project")
	cwd := filepath.Join(project, "apps", "api")
	if err := os.MkdirAll(filepath.Join(project, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(cwd, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)

	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"shared": {
				"type": "openai",
				"api_key": "global-key",
				"models": {"gpt-5-mini": {}}
			}
		},
		"default": {
			"provider": "shared",
			"model": "gpt-5-mini"
		}
	}`)
	writeJSON(t, filepath.Join(project, ".mycode", "config.json"), `{
		"default": {
			"provider": "shared",
			"model": "gpt-5.4"
		},
		"providers": {
			"shared": {
				"base_url": "https://root.example/v1",
				"models": {"gpt-5.4": {}}
			}
		}
	}`)

	settings, err := Load(cwd)
	if err != nil {
		t.Fatal(err)
	}
	if settings.CWD != filepath.Clean(cwd) || settings.WorkspaceRoot != filepath.Clean(project) {
		t.Fatalf("unexpected settings: %#v", settings)
	}
	if settings.DefaultProvider != "shared" || settings.DefaultModel != "gpt-5.4" {
		t.Fatalf("unexpected defaults: %#v", settings)
	}
	if settings.Providers["shared"].APIKey != "global-key" || settings.Providers["shared"].BaseURL != "https://root.example/v1" {
		t.Fatalf("unexpected provider: %#v", settings.Providers["shared"])
	}
	if len(settings.Providers["shared"].Models) != 1 {
		t.Fatalf("unexpected models: %#v", settings.Providers["shared"].Models)
	}
}

func TestLoadCompactThresholdParsing(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	if settings.CompactThreshold != defaultCompactThreshold {
		t.Fatalf("unexpected default compact threshold: %v", settings.CompactThreshold)
	}

	writeJSON(t, filepath.Join(home, "config.json"), `{"default":{"compact_threshold":false}}`)
	settings, err = Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	if settings.CompactThreshold != 0 {
		t.Fatalf("unexpected compact threshold: %v", settings.CompactThreshold)
	}
}

func TestResolveProviderAutoDiscoveryOrder(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENAI_API_KEY", "openai-env-key")
	t.Setenv("MOONSHOT_API_KEY", "moonshot-env-key")

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ProviderName != "openai" || resolved.ProviderType != "openai" {
		t.Fatalf("unexpected provider: %#v", resolved)
	}
}

func TestResolveProviderAutoDiscoveryMatchesPythonRegistryOrder(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("GOOGLE_API_KEY", "google-env-key")
	t.Setenv("DEEPSEEK_API_KEY", "deepseek-env-key")

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ProviderName != "google" || resolved.ProviderType != "google" {
		t.Fatalf("unexpected provider: %#v", resolved)
	}
}

func TestResolveProviderPrefersFirstConfiguredProviderWithCredentials(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENROUTER_API_KEY", "router-env-key")
	t.Setenv("DEEPSEEK_API_KEY", "deepseek-env-key")

	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"openrouter": {
				"models": {"openai/gpt-5": {}}
			},
			"deepseek": {
				"models": {"deepseek-chat": {}}
			}
		}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ProviderName != "openrouter" || resolved.ProviderType != "openrouter" || resolved.APIKey != "router-env-key" {
		t.Fatalf("unexpected provider: %#v", resolved)
	}
}

func TestResolveProviderPreservesConfiguredModelOrder(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENAI_API_KEY", "env-key")

	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"openai": {
				"models": {
					"z-model": {},
					"a-model": {}
				}
			}
		},
		"default": {"provider": "openai"}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	if got := settings.Providers["openai"].ModelOrder; len(got) != 2 || got[0] != "z-model" || got[1] != "a-model" {
		t.Fatalf("unexpected model order: %#v", got)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Model != "z-model" {
		t.Fatalf("unexpected model: %#v", resolved)
	}
}

func TestResolveProviderPrefersExplicitAPIKeyOverEnv(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("ANTHROPIC_API_KEY", "env-key")

	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"claude": {
				"type": "anthropic",
				"api_key": "config-key",
				"models": {"claude-sonnet-4-6": {}}
			}
		},
		"default": {"provider": "claude"}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "request-key", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.APIKey != "request-key" {
		t.Fatalf("unexpected api key: %#v", resolved)
	}
}

func TestResolveProviderOpenAIChatIgnoresReasoningEffort(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENROUTER_API_KEY", "router-env-key")
	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"router": {
				"type": "openai_chat",
				"api_key": "${OPENROUTER_API_KEY}",
				"base_url": "https://openrouter.ai/api/v1",
				"models": {"openai/gpt-5": {}},
				"reasoning_effort": "high"
			}
		},
		"default": {"provider": "router"}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ProviderType != "openai_chat" || resolved.ReasoningEffort != "" {
		t.Fatalf("unexpected provider: %#v", resolved)
	}
}

func TestResolveProviderDefaultProviderDoesNotFallback(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENAI_API_KEY", "openai-env-key")
	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"claude": {
				"type": "anthropic",
				"models": {"claude-sonnet-4-6": {}}
			}
		},
		"default": {"provider": "claude"}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveProvider(settings, "", "", "", "", ""); err == nil || !contains(err.Error(), `provider "claude" is selected`) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestLoadRejectsCustomProviderWithoutType(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"custom-provider": {
				"base_url": "https://custom-endpoint.example/v1"
			}
		}
	}`)

	if _, err := Load(workspace); err == nil || !contains(err.Error(), `provider "custom-provider" must set 'type'`) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestResolveProviderMissingConfiguredAPIKeyEnvVar(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENAI_API_KEY", "default-env-key")
	t.Setenv("OPENROUTER_API_KEY", "")
	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"router": {
				"type": "openai_chat",
				"api_key": "${OPENROUTER_API_KEY}",
				"base_url": "https://openrouter.ai/api/v1",
				"models": {"openai/gpt-5": {}}
			}
		},
		"default": {"provider": "router"}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveProvider(settings, "", "", "", "", ""); err == nil || !contains(err.Error(), "OPENROUTER_API_KEY") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestResolveProviderUsesMetadataOverrides(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENAI_API_KEY", "env-key")

	oldLookup := lookupModelMetadata
	lookupModelMetadata = func(providerType, model string) *models.Metadata {
		return &models.Metadata{
			Provider:           providerType,
			Model:              model,
			ContextWindow:      400000,
			MaxOutputTokens:    128000,
			SupportsReasoning:  boolPtr(true),
			SupportsImageInput: boolPtr(false),
		}
	}
	t.Cleanup(func() {
		lookupModelMetadata = oldLookup
	})

	writeJSON(t, filepath.Join(home, "config.json"), `{
		"providers": {
			"openai": {
				"models": {
					"gpt-5.4": {
						"context_window": 500000,
						"max_output_tokens": 64000,
						"supports_reasoning": false,
						"supports_image_input": true
					}
				}
			}
		},
		"default": {"provider": "openai"}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ContextWindow != 500000 || resolved.MaxTokens != 64000 || resolved.SupportsReasoning || !resolved.SupportsImageInput {
		t.Fatalf("unexpected resolved provider: %#v", resolved)
	}
}

func TestResolveProviderUsesGlobalDefaultReasoningEffort(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home", ".mycode")
	workspace := filepath.Join(root, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MYCODE_HOME", home)
	t.Setenv("OPENAI_API_KEY", "env-key")

	oldLookup := lookupModelMetadata
	lookupModelMetadata = func(providerType, model string) *models.Metadata {
		return &models.Metadata{
			Provider:          providerType,
			Model:             model,
			ContextWindow:     400000,
			MaxOutputTokens:   128000,
			SupportsReasoning: boolPtr(true),
		}
	}
	t.Cleanup(func() {
		lookupModelMetadata = oldLookup
	})

	writeJSON(t, filepath.Join(home, "config.json"), `{
		"default": {
			"provider": "openai",
			"reasoning_effort": "high"
		}
	}`)

	settings, err := Load(workspace)
	if err != nil {
		t.Fatal(err)
	}
	resolved, err := ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.ProviderType != "openai" || resolved.ReasoningEffort != "high" {
		t.Fatalf("unexpected resolved provider: %#v", resolved)
	}
}

func writeJSON(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func boolPtr(v bool) *bool {
	return &v
}

func contains(text, needle string) bool {
	return strings.Contains(text, needle)
}
