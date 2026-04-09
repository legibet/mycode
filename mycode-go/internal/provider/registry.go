package provider

var adapters map[string]Adapter

func init() {
	list := []Adapter{
		newAnthropicAdapter("anthropic"),
		newAnthropicAdapter("moonshotai"),
		newAnthropicAdapter("minimax"),
		newOpenAIResponsesAdapter(),
		newOpenAIChatAdapter("openai_chat"),
		newOpenAIChatAdapter("deepseek"),
		newOpenAIChatAdapter("zai"),
		newOpenAIChatAdapter("openrouter"),
		newGoogleAdapter(),
	}
	adapters = make(map[string]Adapter, len(list))
	for _, a := range list {
		adapters[a.Spec().ID] = a
	}
}

// LookupAdapter returns one registered provider adapter.
func LookupAdapter(id string) (Adapter, bool) {
	adapter, ok := adapters[id]
	return adapter, ok
}
