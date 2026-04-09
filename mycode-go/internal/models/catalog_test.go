package models

import (
	"sync"
	"testing"
)

func TestLookupPrefersCurrentProviderFamily(t *testing.T) {
	withCatalog(t, `{
		"openai": {"gpt-5": {"max_output_tokens": 128000, "supports_reasoning": true, "supports_image_input": true}},
		"openrouter": {"openai/gpt-5": {"max_output_tokens": 64000, "supports_reasoning": true}}
	}`, func() {
		meta := Lookup("openrouter", "openai/gpt-5")
		if meta == nil || meta.Provider != "openrouter" || meta.MaxOutputTokens != 64000 {
			t.Fatalf("unexpected metadata: %#v", meta)
		}
	})
}

func TestLookupFallsBackToCanonicalProvider(t *testing.T) {
	withCatalog(t, `{
		"openai": {"gpt-5": {"max_output_tokens": 128000, "supports_reasoning": true, "supports_image_input": true}},
		"other": {}
	}`, func() {
		meta := Lookup("openai_chat", "openai/gpt-5")
		if meta == nil || meta.Provider != "openai" || meta.Model != "gpt-5" {
			t.Fatalf("unexpected metadata: %#v", meta)
		}
		if meta.SupportsImageInput == nil || !*meta.SupportsImageInput {
			t.Fatalf("unexpected metadata: %#v", meta)
		}
	})
}

func TestLookupFallsBackToAIHubMix(t *testing.T) {
	withCatalog(t, `{
		"aihubmix": {"glm-5.1": {"max_output_tokens": 131072}}
	}`, func() {
		meta := Lookup("zai", "glm-5.1")
		if meta == nil || meta.Provider != "aihubmix" || meta.MaxOutputTokens != 131072 {
			t.Fatalf("unexpected metadata: %#v", meta)
		}
	})
}

func TestLookupReadsImageAndPDFSupport(t *testing.T) {
	withCatalog(t, `{
		"openai": {
			"gpt-5.4": {
				"max_output_tokens": 128000,
				"supports_reasoning": true,
				"supports_image_input": true,
				"supports_pdf_input": true
			}
		}
	}`, func() {
		meta := Lookup("openai", "gpt-5.4")
		if meta == nil || meta.SupportsImageInput == nil || meta.SupportsPDFInput == nil {
			t.Fatalf("unexpected metadata: %#v", meta)
		}
		if !*meta.SupportsImageInput || !*meta.SupportsPDFInput {
			t.Fatalf("unexpected metadata: %#v", meta)
		}
	})
}

func TestLookupDoesNotRetryAfterFirstCatalogLoad(t *testing.T) {
	withCatalog(t, `{"zai": {}}`, func() {
		if meta := Lookup("zai", "glm-5.1"); meta != nil {
			t.Fatalf("unexpected metadata: %#v", meta)
		}

		catalogJSON = []byte(`{"aihubmix": {"glm-5.1": {"max_output_tokens": 131072}}}`)
		if meta := Lookup("zai", "glm-5.1"); meta != nil {
			t.Fatalf("unexpected metadata after cached miss: %#v", meta)
		}
	})
}

func withCatalog(t *testing.T, raw string, fn func()) {
	t.Helper()

	originalJSON := append([]byte(nil), catalogJSON...)
	originalCatalog := catalog
	catalogJSON = []byte(raw)
	catalog = nil
	loadOnce = sync.Once{}

	t.Cleanup(func() {
		catalogJSON = originalJSON
		catalog = originalCatalog
		loadOnce = sync.Once{}
	})

	fn()
}
