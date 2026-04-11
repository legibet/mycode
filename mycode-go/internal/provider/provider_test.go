package provider

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	anthropic "github.com/anthropics/anthropic-sdk-go"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/tools"
	"github.com/openai/openai-go/v3/responses"
	"google.golang.org/genai"
)

var pdfBytes = []byte("%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")
var pngBytes = mustBase64Decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1X8AAAAASUVORK5CYII=")

func TestRepairMessagesForReplayDowngradesUnsupportedPDF(t *testing.T) {
	replay := RepairMessagesForReplay([]message.Message{
		message.BuildMessage("user", []message.Block{
			message.TextBlock("check this", nil),
			message.DocumentBlock(base64.StdEncoding.EncodeToString(pdfBytes), "application/pdf", `report <"draft">.pdf`, nil),
		}, nil),
	}, true, false)

	if len(replay) != 1 || len(replay[0].Content) != 2 {
		t.Fatalf("unexpected replay: %#v", replay)
	}
	if replay[0].Content[1].Type != "text" || replay[0].Content[1].Text != `<file name="report &lt;&quot;draft&quot;&gt;.pdf" media_type="application/pdf" kind="document">Current model does not support PDF input.</file>` {
		t.Fatalf("unexpected attachment downgrade: %#v", replay[0].Content[1])
	}
}

func TestAllBuiltInProvidersRegisterAdapters(t *testing.T) {
	for _, spec := range Specs() {
		adapter, ok := LookupAdapter(spec.ID)
		if !ok {
			t.Fatalf("missing adapter for provider %q", spec.ID)
		}
		if adapter.Spec().ID != spec.ID {
			t.Fatalf("adapter %q registered as %q", spec.ID, adapter.Spec().ID)
		}
	}
}

func TestOpenAIChatPayloadUsesExtraBody(t *testing.T) {
	zai := newOpenAIChatAdapter("zai").(openAIChatAdapter)
	zaiPayload := zai.buildPayload(Request{
		Model: "glm-5.1",
	})
	if _, ok := zaiPayload["thinking"]; ok {
		t.Fatalf("unexpected top-level thinking: %#v", zaiPayload)
	}
	extraBody, _ := zaiPayload["extra_body"].(map[string]any)
	if extraBody == nil {
		t.Fatalf("missing extra_body: %#v", zaiPayload)
	}
	thinking, _ := extraBody["thinking"].(map[string]any)
	if thinking == nil || thinking["type"] != "enabled" || thinking["clear_thinking"] != false {
		t.Fatalf("unexpected extra_body thinking: %#v", zaiPayload)
	}

	openRouter := newOpenAIChatAdapter("openrouter").(openAIChatAdapter)
	routerPayload := openRouter.buildPayload(Request{
		Model:           "openai/gpt-5",
		ReasoningEffort: "high",
	})
	if _, ok := routerPayload["reasoning"]; ok {
		t.Fatalf("unexpected top-level reasoning: %#v", routerPayload)
	}
	routerExtra, _ := routerPayload["extra_body"].(map[string]any)
	if routerExtra == nil {
		t.Fatalf("missing extra_body: %#v", routerPayload)
	}
	reasoning, _ := routerExtra["reasoning"].(map[string]any)
	if reasoning == nil || reasoning["effort"] != "high" {
		t.Fatalf("unexpected extra_body reasoning: %#v", routerPayload)
	}
}

func TestAnthropicSerializeBlockReplaysCaller(t *testing.T) {
	adapter := newAnthropicAdapter("anthropic").(anthropicAdapter)
	block := message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, map[string]any{
		"native": map[string]any{"caller": "assistant"},
	})
	payload := adapter.serializeBlock(block)
	if payload["caller"] != "assistant" {
		t.Fatalf("unexpected payload: %#v", payload)
	}
}

func TestAnthropicConvertMessageKeepsStopSequenceAndServiceTier(t *testing.T) {
	adapter := newAnthropicAdapter("anthropic").(anthropicAdapter)
	raw := []byte(`{
		"id":"msg_123",
		"model":"claude-sonnet-4-6",
		"stop_reason":"tool_use",
		"stop_sequence":"stop-here",
		"usage":{"input_tokens":10,"output_tokens":5,"service_tier":"priority"},
		"content":[
			{"type":"tool_use","id":"call_1","name":"read","input":{"path":"x.py"},"caller":"assistant"}
		]
	}`)
	var msg anthropic.Message
	if err := json.Unmarshal(raw, &msg); err != nil {
		t.Fatal(err)
	}
	converted := adapter.convertMessage(msg)
	native, _ := converted.Meta["native"].(map[string]any)
	if native["stop_sequence"] != "stop-here" || native["service_tier"] != "priority" {
		t.Fatalf("unexpected native meta: %#v", converted.Meta)
	}
}

func TestOpenAIResponsesSerializeToolMakesOptionalFieldsNullable(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	readTool := adapter.serializeTool(map[string]any{
		"name":         tools.DefaultSpecs()[0].Name,
		"description":  tools.DefaultSpecs()[0].Description,
		"input_schema": tools.DefaultSpecs()[0].InputSchema,
	})
	parameters, _ := readTool["parameters"].(map[string]any)
	properties, _ := parameters["properties"].(map[string]any)
	offset, _ := properties["offset"].(map[string]any)
	limit, _ := properties["limit"].(map[string]any)
	if readTool["strict"] != true {
		t.Fatalf("unexpected tool: %#v", readTool)
	}
	if got := offset["type"]; !containsAnyString(got, "null") {
		t.Fatalf("offset should be nullable: %#v", readTool)
	}
	if got := limit["type"]; !containsAnyString(got, "null") {
		t.Fatalf("limit should be nullable: %#v", readTool)
	}
}

func TestOpenAIResponsesBuildPayloadIncludesPromptCacheKey(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	payload := adapter.buildPayload(Request{
		Model:     "gpt-5.4",
		SessionID: "session_123",
		Messages: []message.Message{
			message.UserTextMessage("hello", nil),
		},
		System:    "You are helpful.",
		MaxTokens: 4096,
	})

	if payload["prompt_cache_key"] != "session_123" {
		t.Fatalf("unexpected payload: %#v", payload)
	}
	if payload["store"] != false {
		t.Fatalf("unexpected payload: %#v", payload)
	}
	include, _ := payload["include"].([]string)
	if len(include) != 1 || include[0] != "reasoning.encrypted_content" {
		t.Fatalf("unexpected payload: %#v", payload)
	}
	if _, ok := payload["previous_response_id"]; ok {
		t.Fatalf("unexpected previous_response_id: %#v", payload)
	}
}

func TestOpenAIResponsesTextStreamIgnoresTrailingEmptyEvent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/responses" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		defer func() { _ = r.Body.Close() }()
		_, _ = io.ReadAll(r.Body)

		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w,
			"event: response.output_text.delta\n"+
				"data: {\"type\":\"response.output_text.delta\",\"content_index\":0,\"delta\":\"OK\",\"item_id\":\"msg_1\",\"logprobs\":[],\"output_index\":1,\"sequence_number\":1}\n\n"+
				"event: response.output_item.done\n"+
				"data: {\"type\":\"response.output_item.done\",\"item\":{\"id\":\"msg_1\",\"type\":\"message\",\"status\":\"completed\",\"content\":[{\"type\":\"output_text\",\"annotations\":[],\"logprobs\":[],\"text\":\"OK\"}],\"phase\":\"final_answer\",\"role\":\"assistant\"},\"output_index\":1,\"sequence_number\":2}\n\n"+
				"event: response.completed\n"+
				"data: {\"type\":\"response.completed\",\"sequence_number\":3,\"response\":{\"id\":\"resp_1\",\"model\":\"gpt-5.4-mini\",\"object\":\"response\",\"output\":[],\"status\":\"completed\"}}\n\n\n",
		)
	}))
	defer server.Close()

	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	stream := adapter.StreamTurn(context.Background(), Request{
		Model:     "gpt-5.4-mini",
		APIKey:    "sk-test",
		APIBase:   server.URL,
		MaxTokens: 64,
		Messages:  []message.Message{message.UserTextMessage("hi", nil)},
	})

	text := strings.Builder{}
	var final *message.Message
	for event := range stream {
		switch event.Type {
		case "provider_error":
			t.Fatalf("unexpected provider error: %v", event.Err)
		case "text_delta":
			text.WriteString(event.Text)
		case "message_done":
			final = event.Msg
		}
	}

	if text.String() != "OK" {
		t.Fatalf("unexpected text delta: %q", text.String())
	}
	if final == nil {
		t.Fatal("missing final message")
	}
	if len(final.Content) != 1 || final.Content[0].Type != "text" || final.Content[0].Text != "OK" {
		t.Fatalf("unexpected final message: %#v", final.Content)
	}
}

func TestOpenAIResponsesToolCallStreamIgnoresTrailingEmptyEvent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() { _ = r.Body.Close() }()
		_, _ = io.ReadAll(r.Body)

		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w,
			"event: response.output_item.done\n"+
				"data: {\"type\":\"response.output_item.done\",\"item\":{\"id\":\"fc_1\",\"type\":\"function_call\",\"call_id\":\"call_1\",\"name\":\"read\",\"arguments\":\"{\\\"path\\\":\\\"x.py\\\"}\",\"status\":\"completed\"},\"output_index\":0,\"sequence_number\":1}\n\n"+
				"event: response.completed\n"+
				"data: {\"type\":\"response.completed\",\"sequence_number\":2,\"response\":{\"id\":\"resp_2\",\"model\":\"gpt-5.4-mini\",\"object\":\"response\",\"output\":[],\"status\":\"completed\"}}\n\n\n",
		)
	}))
	defer server.Close()

	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	stream := adapter.StreamTurn(context.Background(), Request{
		Model:     "gpt-5.4-mini",
		APIKey:    "sk-test",
		APIBase:   server.URL,
		MaxTokens: 64,
		Messages:  []message.Message{message.UserTextMessage("use a tool", nil)},
	})

	var final *message.Message
	for event := range stream {
		if event.Type == "provider_error" {
			t.Fatalf("unexpected provider error: %v", event.Err)
		}
		if event.Type == "message_done" {
			final = event.Msg
		}
	}

	if final == nil {
		t.Fatal("missing final message")
	}
	if len(final.Content) != 1 {
		t.Fatalf("unexpected final blocks: %#v", final.Content)
	}
	block := final.Content[0]
	if block.Type != "tool_use" || block.ID != "call_1" || block.Name != "read" {
		t.Fatalf("unexpected tool block: %#v", block)
	}
	if path, _ := block.Input["path"].(string); path != "x.py" {
		t.Fatalf("unexpected tool input: %#v", block.Input)
	}
}

func TestOpenAIResponsesReplaysNativeOutputItemsForToolResults(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	payload := adapter.buildPayload(Request{
		Model: "gpt-5.4",
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
			}, "openai", "gpt-5.4", "", "", nil, map[string]any{
				"output_items": []any{
					map[string]any{
						"type":              "reasoning",
						"id":                "rs_1",
						"status":            "completed",
						"summary":           []any{},
						"encrypted_content": "enc_1",
					},
					map[string]any{
						"type":    "message",
						"id":      "msg_1",
						"role":    "assistant",
						"phase":   "commentary",
						"status":  "completed",
						"content": []any{map[string]any{"type": "output_text", "text": "Checking the file."}},
					},
					map[string]any{
						"type":      "function_call",
						"id":        "fc_1",
						"call_id":   "call_1",
						"name":      "read",
						"arguments": `{"path":"x.py"}`,
						"status":    "completed",
					},
				},
			}),
			message.BuildMessage("user", []message.Block{
				message.ToolResultBlock("call_1", "file contents", "file contents", false, nil, nil),
			}, nil),
		},
		MaxTokens: 4096,
	})

	input := payload["input"].([]any)
	expected := []any{
		map[string]any{"type": "reasoning", "id": "rs_1", "summary": []any{}, "encrypted_content": "enc_1"},
		map[string]any{
			"type":    "message",
			"role":    "assistant",
			"phase":   "commentary",
			"content": []any{map[string]any{"type": "output_text", "text": "Checking the file."}},
		},
		map[string]any{
			"type":      "function_call",
			"call_id":   "call_1",
			"name":      "read",
			"arguments": `{"path":"x.py"}`,
		},
		map[string]any{"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
	}
	if !reflect.DeepEqual(input, expected) {
		t.Fatalf("unexpected input items:\n got: %#v\nwant: %#v", input, expected)
	}
}

func TestOpenAIResponsesFallbackReplaySkipsReasoningBlocks(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	payload := adapter.buildPayload(Request{
		Model: "gpt-5.4",
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ThinkingBlock("Need the tool first.", nil),
				message.TextBlock("I will inspect the file.", nil),
				message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
			}, "openai", "gpt-5.4", "", "", nil, nil),
		},
		MaxTokens: 4096,
	})

	input := payload["input"].([]any)
	expected := []any{
		map[string]any{
			"type": "message",
			"role": "assistant",
			"content": []map[string]any{{
				"type": "output_text",
				"text": "I will inspect the file.",
			}},
		},
		map[string]any{
			"type":      "function_call",
			"call_id":   "call_1",
			"name":      "read",
			"arguments": `{"path":"x.py"}`,
		},
		map[string]any{
			"type":    "function_call_output",
			"call_id": "call_1",
			"output":  "error: tool call was interrupted",
		},
	}
	if !reflect.DeepEqual(input, expected) {
		t.Fatalf("unexpected input items:\n got: %#v\nwant: %#v", input, expected)
	}
}

func TestUserPDFInputSerialization(t *testing.T) {
	pdfData := base64.StdEncoding.EncodeToString(pdfBytes)
	request := Request{
		Model:              "gpt-5.4",
		SupportsImageInput: true,
		SupportsPDFInput:   true,
		Messages: []message.Message{
			message.BuildMessage("user", []message.Block{
				message.TextBlock("summarize", nil),
				message.DocumentBlock(pdfData, "application/pdf", "report.pdf", nil),
			}, nil),
		},
	}

	responsesAdapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	responsesPayload := responsesAdapter.buildPayload(request)
	input := responsesPayload["input"].([]any)
	content := input[0].(map[string]any)["content"].([]any)
	fileInput, _ := content[1].(map[string]any)
	if fileInput["type"] != "input_file" || fileInput["filename"] != "report.pdf" || fileInput["file_data"] != "data:application/pdf;base64,"+pdfData {
		t.Fatalf("unexpected responses content: %#v", content)
	}

	chatAdapter := newOpenAIChatAdapter("openai_chat").(openAIChatAdapter)
	chatPayload := chatAdapter.buildPayload(request)
	chatMessages := chatPayload["messages"].([]any)
	chatContent := chatMessages[0].(map[string]any)["content"].([]any)
	filePart := chatContent[1].(map[string]any)
	if filePart["type"] != "file" {
		t.Fatalf("unexpected chat content: %#v", chatContent)
	}

	anthropicAdapter := newAnthropicAdapter("anthropic").(anthropicAdapter)
	anthropicPayload := anthropicAdapter.buildPayload(Request{
		Model:              "claude-sonnet-4-6",
		SupportsImageInput: true,
		SupportsPDFInput:   true,
		Messages:           request.Messages,
	})
	anthropicMessages := anthropicPayload["messages"].([]map[string]any)
	anthropicContent := anthropicMessages[0]["content"].([]map[string]any)
	if anthropicContent[1]["type"] != "document" {
		t.Fatalf("unexpected anthropic content: %#v", anthropicContent)
	}

	googleAdapter := newGoogleAdapter().(googleAdapter)
	googleContent := googleAdapter.buildContents(Request{
		Model:              "gemini-3-flash-preview",
		SupportsImageInput: true,
		SupportsPDFInput:   true,
		Messages:           request.Messages,
	})
	if googleContent[0].Parts[1].InlineData == nil || googleContent[0].Parts[1].InlineData.MIMEType != "application/pdf" {
		t.Fatalf("unexpected google content: %#v", googleContent)
	}
}

func TestRepairMessagesForReplayDropsDuplicateAndOrphanToolResults(t *testing.T) {
	replay := RepairMessagesForReplay([]message.Message{
		message.AssistantMessage([]message.Block{
			message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
		}, "", "", "", "", nil, nil),
		message.BuildMessage("user", []message.Block{
			message.ToolResultBlock("call_1", "first", "first", false, nil, nil),
			message.ToolResultBlock("call_1", "duplicate", "duplicate", false, nil, nil),
			message.ToolResultBlock("call_2", "orphan", "orphan", false, nil, nil),
		}, nil),
		message.AssistantMessage([]message.Block{
			message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
		}, "", "", "", "", nil, nil),
	}, true, true)

	expected := []message.Message{
		message.AssistantMessage([]message.Block{
			message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
		}, "", "", "", "", nil, nil),
		message.BuildMessage("user", []message.Block{
			message.ToolResultBlock("call_1", "first", "first", false, nil, nil),
		}, nil),
	}
	if !reflect.DeepEqual(replay, expected) {
		t.Fatalf("unexpected replay:\n got: %#v\nwant: %#v", replay, expected)
	}
}

func TestRepairMessagesForReplayKeepsPlaceholderUserTurn(t *testing.T) {
	replay := RepairMessagesForReplay([]message.Message{
		message.AssistantMessage([]message.Block{
			message.TextBlock("first", nil),
		}, "", "", "", "", nil, nil),
		message.BuildMessage("user", []message.Block{
			message.ToolResultBlock("missing", "orphan", "orphan", false, nil, nil),
		}, nil),
		message.AssistantMessage([]message.Block{
			message.TextBlock("second", nil),
		}, "", "", "", "", nil, nil),
	}, true, true)

	expected := []message.Message{
		message.AssistantMessage([]message.Block{message.TextBlock("first", nil)}, "", "", "", "", nil, nil),
		message.BuildMessage("user", []message.Block{
			message.TextBlock("[User turn omitted during replay]", nil),
		}, map[string]any{"synthetic": true}),
		message.AssistantMessage([]message.Block{message.TextBlock("second", nil)}, "", "", "", "", nil, nil),
	}
	if !reflect.DeepEqual(replay, expected) {
		t.Fatalf("unexpected replay:\n got: %#v\nwant: %#v", replay, expected)
	}
}

func TestRepairMessagesForReplayFiltersImagesWhenDisabled(t *testing.T) {
	replay := RepairMessagesForReplay([]message.Message{
		message.AssistantMessage([]message.Block{
			message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.png"}, nil),
		}, "", "", "", "", nil, nil),
		message.BuildMessage("user", []message.Block{
			message.TextBlock("describe this", nil),
			message.ImageBlock("abc", "image/png", `logo"<v2>.png`, nil),
			message.ToolResultBlock("call_1", "Read image file [image/png]", "Read image file [image/png]", false, []message.Block{
				message.TextBlock("Read image file [image/png]", nil),
				message.ImageBlock("abc", "image/png", "", nil),
			}, nil),
		}, nil),
	}, false, true)

	expected := []message.Message{
		message.AssistantMessage([]message.Block{
			message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.png"}, nil),
		}, "", "", "", "", nil, nil),
		message.BuildMessage("user", []message.Block{
			message.TextBlock("describe this", nil),
			message.TextBlock(`<file name="logo&quot;&lt;v2&gt;.png" media_type="image/png" kind="image">Current model does not support image input.</file>`, map[string]any{"attachment": true}),
			message.ToolResultBlock("call_1", "Read image file [image/png]", "Read image file [image/png]", false, []message.Block{
				message.TextBlock("Read image file [image/png]", nil),
			}, nil),
		}, nil),
	}
	if !reflect.DeepEqual(replay, expected) {
		t.Fatalf("unexpected replay:\n got: %#v\nwant: %#v", replay, expected)
	}
}

func TestOpenAIResponsesFallsBackToFullReplayForCrossProviderHistory(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	payload := adapter.buildPayload(Request{
		Model: "gpt-5.4",
		Messages: []message.Message{
			message.UserTextMessage("double 21", nil),
			message.AssistantMessage([]message.Block{
				message.ThinkingBlock("Need the tool first.", nil),
				message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
			}, "anthropic", "claude-sonnet-4-6", "", "", nil, nil),
			message.BuildMessage("user", []message.Block{
				message.ToolResultBlock("call_1", "42", "42", false, nil, nil),
			}, nil),
		},
		MaxTokens: 4096,
	})

	expected := []any{
		map[string]any{
			"type": "message",
			"role": "user",
			"content": []any{
				map[string]any{"type": "input_text", "text": "double 21"},
			},
		},
		map[string]any{
			"type":      "function_call",
			"call_id":   "call_1",
			"name":      "read",
			"arguments": `{"path":"x.py"}`,
		},
		map[string]any{
			"type":    "function_call_output",
			"call_id": "call_1",
			"output":  "42",
		},
	}
	if !reflect.DeepEqual(payload["input"], expected) {
		t.Fatalf("unexpected replay input:\n got: %#v\nwant: %#v", payload["input"], expected)
	}
}

func TestOpenAIResponsesSerializesToolResultImages(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	payload := adapter.buildPayload(Request{
		Model:              "gpt-5.4",
		SupportsImageInput: true,
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.png"}, nil),
			}, "", "", "", "", nil, nil),
			message.BuildMessage("user", []message.Block{
				message.ToolResultBlock("call_1", "Read image file [image/png]", "Read image file [image/png]", false, []message.Block{
					message.TextBlock("Read image file [image/png]", nil),
					message.ImageBlock(base64.StdEncoding.EncodeToString(pngBytes), "image/png", "tiny.png", nil),
				}, nil),
			}, nil),
		},
		MaxTokens: 4096,
	})

	input := payload["input"].([]any)
	first := input[0].(map[string]any)
	second := input[1].(map[string]any)
	if first["type"] != "function_call" || second["type"] != "function_call_output" {
		t.Fatalf("unexpected input: %#v", input)
	}
	output, _ := second["output"].([]any)
	if len(output) != 2 {
		t.Fatalf("unexpected output: %#v", second["output"])
	}
	if !reflect.DeepEqual(output[0], map[string]any{"type": "input_text", "text": "Read image file [image/png]"}) {
		t.Fatalf("unexpected output: %#v", output)
	}
	image, _ := output[1].(map[string]any)
	if image["type"] != "input_image" {
		t.Fatalf("unexpected output: %#v", output)
	}
}

func TestOpenAIResponsesConvertsFinalResponseBlocks(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	response := mustUnmarshalResponse(t, `{
		"id":"resp_123",
		"model":"gpt-5.4",
		"status":"completed",
		"usage":{"input_tokens":10,"output_tokens":5},
		"output":[
			{"type":"reasoning","id":"rs_1","status":"completed","content":[{"text":"think"}],"summary":[]},
			{"type":"message","content":[{"type":"output_text","text":"answer","annotations":[]}]},
			{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\"path\": \"x.py\"}","status":"completed"}
		]
	}`)

	msg := adapter.convertResponse(response, nil)
	if msg.Role != "assistant" || len(msg.Content) != 3 {
		t.Fatalf("unexpected message: %#v", msg)
	}
	if msg.Content[0].Type != "thinking" || msg.Content[0].Text != "think" {
		t.Fatalf("unexpected message: %#v", msg)
	}
	if !reflect.DeepEqual(msg.Content[1], message.TextBlock("answer", map[string]any{"native": map[string]any{"annotations": []any{}}})) {
		t.Fatalf("unexpected text block: %#v", msg.Content[1])
	}
	if msg.Content[2].Type != "tool_use" || msg.Content[2].ID != "call_1" || !reflect.DeepEqual(msg.Content[2].Input, map[string]any{"path": "x.py"}) {
		t.Fatalf("unexpected tool block: %#v", msg.Content[2])
	}
	native, _ := msg.Meta["native"].(map[string]any)
	outputItems, _ := native["output_items"].([]any)
	if len(outputItems) != 3 {
		t.Fatalf("unexpected native meta: %#v", msg.Meta)
	}
	first, _ := outputItems[0].(map[string]any)
	if _, ok := first["acknowledged_safety_checks"]; ok {
		t.Fatalf("unexpected union junk in stored output item: %#v", first)
	}
	replayPayload := adapter.buildPayload(Request{
		Model:     "gpt-5.4",
		Messages:  []message.Message{msg},
		MaxTokens: 4096,
	})
	replayInput := replayPayload["input"].([]any)
	replayFirst, _ := replayInput[0].(map[string]any)
	if _, ok := replayFirst["acknowledged_safety_checks"]; ok {
		t.Fatalf("unexpected replay item: %#v", replayFirst)
	}
}

func TestOpenAIResponsesUsesStreamOutputItemsWhenFinalOutputIsEmpty(t *testing.T) {
	adapter := newOpenAIResponsesAdapter().(openAIResponsesAdapter)
	response := mustUnmarshalResponse(t, `{
		"id":"resp_123",
		"model":"gpt-5.4",
		"status":"completed",
		"usage":{"input_tokens":10,"output_tokens":5},
		"output":[]
	}`)
	outputItems := mustUnmarshalOutputItems(t, `[
		{"type":"message","content":[{"type":"output_text","text":"hello world","annotations":[]}]},
		{"type":"function_call","id":"fc_1","call_id":"call_1","name":"read","arguments":"{\"path\":\"pyproject.toml\"}","status":"completed"}
	]`)

	msg := adapter.convertResponse(response, outputItems)
	if !reflect.DeepEqual(msg.Content, []message.Block{
		message.TextBlock("hello world", map[string]any{"native": map[string]any{"annotations": []any{}}}),
		message.ToolUseBlock("call_1", "read", map[string]any{"path": "pyproject.toml"}, map[string]any{"native": map[string]any{"item_id": "fc_1", "status": "completed"}}),
	}) {
		t.Fatalf("unexpected message: %#v", msg)
	}
}

func TestOpenAIChatExtractsReasoningFromKnownExtraFields(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		text string
		meta map[string]any
	}{
		{
			name: "root reasoning_content",
			raw:  `{"reasoning_content":"step zero"}`,
			text: "step zero",
			meta: map[string]any{"reasoning_field": "reasoning_content"},
		},
		{
			name: "model_extra reasoning_content",
			raw:  `{"model_extra":{"reasoning_content":"step one"}}`,
			text: "step one",
			meta: map[string]any{"reasoning_field": "reasoning_content"},
		},
		{
			name: "model_extra reasoning_details",
			raw:  `{"model_extra":{"reasoning_details":[{"type":"reasoning.text","text":"step "},{"type":"reasoning.text","text":"two"}]}}`,
			text: "step two",
			meta: map[string]any{
				"reasoning_field": "reasoning_details",
				"reasoning_details": []any{
					map[string]any{"type": "reasoning.text", "text": "step "},
					map[string]any{"type": "reasoning.text", "text": "two"},
				},
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			text, meta := extractChatReasoningDelta(tc.raw)
			if text != tc.text || !reflect.DeepEqual(meta, tc.meta) {
				t.Fatalf("unexpected reasoning delta: %q %#v", text, meta)
			}
		})
	}
}

func TestOpenAIChatReplaysReasoningByDefault(t *testing.T) {
	adapter := newOpenAIChatAdapter("openai_chat").(openAIChatAdapter)
	payload := adapter.buildPayload(Request{
		Model: "test-model",
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ThinkingBlock("think", nil),
				message.TextBlock("answer", nil),
			}, "", "", "", "", nil, nil),
		},
		MaxTokens: 2048,
	})

	messages := payload["messages"].([]any)
	first, _ := messages[0].(map[string]any)
	if first["reasoning_content"] != "think" {
		t.Fatalf("unexpected payload: %#v", payload)
	}
}

func TestDeepSeekReplaysReasoningAcrossTurns(t *testing.T) {
	adapter := newOpenAIChatAdapter("deepseek").(openAIChatAdapter)

	withToolCall := adapter.buildPayload(Request{
		Model: "test-model",
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ThinkingBlock("think", map[string]any{"native": map[string]any{"reasoning_field": "reasoning_content"}}),
				message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
			}, "", "", "", "", nil, nil),
			message.BuildMessage("user", []message.Block{
				message.ToolResultBlock("call_1", "done", "done", false, nil, nil),
			}, nil),
		},
		MaxTokens: 2048,
	})
	first, _ := withToolCall["messages"].([]any)[0].(map[string]any)
	if first["reasoning_content"] != "think" {
		t.Fatalf("unexpected payload: %#v", withToolCall)
	}

	withNextUserTurn := adapter.buildPayload(Request{
		Model: "test-model",
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ThinkingBlock("think", map[string]any{"native": map[string]any{"reasoning_field": "reasoning_content"}}),
				message.TextBlock("done", nil),
			}, "", "", "", "", nil, nil),
			message.UserTextMessage("next question", nil),
		},
		MaxTokens: 2048,
	})
	first, _ = withNextUserTurn["messages"].([]any)[0].(map[string]any)
	if first["reasoning_content"] != "think" {
		t.Fatalf("unexpected payload: %#v", withNextUserTurn)
	}
}

func TestAnthropicPrepareMessagesNormalizesToolIDs(t *testing.T) {
	adapter := newAnthropicAdapter("anthropic").(anthropicAdapter)
	prepared := prepareMessages(Request{
		Model: "claude-sonnet-4-6",
		Messages: []message.Message{
			message.AssistantMessage([]message.Block{
				message.ToolUseBlock("a/b", "read", map[string]any{"path": "x.py"}, nil),
				message.ToolUseBlock("a|b", "write", map[string]any{"path": "y.py"}, nil),
			}, "anthropic", "claude-sonnet-4-6", "", "", nil, nil),
			message.BuildMessage("user", []message.Block{
				message.ToolResultBlock("a/b", "done a", "done a", false, nil, nil),
				message.ToolResultBlock("a|b", "done b", "done b", false, nil, nil),
			}, nil),
		},
	}, adapter.projectToolCallID)

	firstID := prepared[0].Content[0].ID
	secondID := prepared[0].Content[1].ID
	if firstID == secondID || len(firstID) == 0 || len(secondID) == 0 {
		t.Fatalf("unexpected prepared messages: %#v", prepared)
	}
	if prepared[1].Content[0].ToolUseID != firstID || prepared[1].Content[1].ToolUseID != secondID {
		t.Fatalf("unexpected prepared messages: %#v", prepared)
	}
}

func TestAnthropicBuildPayloadMapsReasoningConfig(t *testing.T) {
	adapter := newAnthropicAdapter("anthropic").(anthropicAdapter)
	cases := []struct {
		model        string
		effort       string
		thinking     map[string]any
		outputConfig map[string]any
	}{
		{
			model:        "claude-sonnet-4-6",
			effort:       "high",
			thinking:     map[string]any{"type": "adaptive"},
			outputConfig: map[string]any{"effort": "high"},
		},
		{
			model:    "claude-opus-4-5",
			effort:   "xhigh",
			thinking: map[string]any{"type": "enabled", "budget_tokens": 32768},
		},
		{
			model:        "claude-opus-4-6",
			effort:       "xhigh",
			thinking:     map[string]any{"type": "adaptive"},
			outputConfig: map[string]any{"effort": "max"},
		},
	}

	for _, tc := range cases {
		t.Run(tc.model+"/"+tc.effort, func(t *testing.T) {
			payload := adapter.buildPayload(Request{
				Model:           tc.model,
				MaxTokens:       8192,
				ReasoningEffort: tc.effort,
			})
			if !reflect.DeepEqual(payload["thinking"], tc.thinking) {
				t.Fatalf("unexpected payload: %#v", payload)
			}
			if tc.outputConfig == nil {
				if _, ok := payload["output_config"]; ok {
					t.Fatalf("unexpected payload: %#v", payload)
				}
				return
			}
			if !reflect.DeepEqual(payload["output_config"], tc.outputConfig) {
				t.Fatalf("unexpected payload: %#v", payload)
			}
		})
	}
}

func TestAnthropicBuildPayloadAddsCacheControlToLatestUserBlock(t *testing.T) {
	adapter := newAnthropicAdapter("anthropic").(anthropicAdapter)
	payload := adapter.buildPayload(Request{
		Model:     "test-model",
		MaxTokens: 4096,
		System:    "You are helpful.",
		Messages: []message.Message{
			message.UserTextMessage("first user message", nil),
			message.AssistantMessage([]message.Block{
				message.TextBlock("assistant reply", nil),
			}, "", "", "", "", nil, nil),
			message.AssistantMessage([]message.Block{
				message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
			}, "", "", "", "", nil, nil),
			message.BuildMessage("user", []message.Block{
				message.TextBlock("latest user message", nil),
				message.ToolResultBlock("call_1", "tool output", "tool output", false, nil, nil),
			}, nil),
		},
	})

	systemBlocks, _ := payload["system"].([]map[string]any)
	if len(systemBlocks) != 1 || !reflect.DeepEqual(systemBlocks[0]["cache_control"], map[string]any{"type": "ephemeral"}) {
		t.Fatalf("unexpected payload: %#v", payload)
	}
	messages := payload["messages"].([]map[string]any)
	firstContent := messages[0]["content"].([]map[string]any)
	if _, ok := firstContent[0]["cache_control"]; ok {
		t.Fatalf("unexpected payload: %#v", payload)
	}
	lastContent := messages[3]["content"].([]map[string]any)
	if !reflect.DeepEqual(lastContent[1]["cache_control"], map[string]any{"type": "ephemeral"}) {
		t.Fatalf("unexpected payload: %#v", payload)
	}
}

func TestGoogleBuildConfigMapsReasoningEffort(t *testing.T) {
	adapter := newGoogleAdapter().(googleAdapter)
	cases := []struct {
		model string
		level genai.ThinkingLevel
	}{
		{model: "gemini-3.1-pro-preview", level: genai.ThinkingLevelLow},
		{model: "gemini-3-flash-preview", level: genai.ThinkingLevelMinimal},
	}

	for _, tc := range cases {
		t.Run(tc.model, func(t *testing.T) {
			config := adapter.buildConfig(Request{
				Model:           tc.model,
				System:          "You are helpful.",
				MaxTokens:       2048,
				ReasoningEffort: "none",
			})
			if config.ThinkingConfig == nil || !config.ThinkingConfig.IncludeThoughts {
				t.Fatalf("unexpected config: %#v", config)
			}
			if config.ThinkingConfig.ThinkingLevel != tc.level {
				t.Fatalf("unexpected config: %#v", config)
			}
		})
	}
}

func TestGoogleBuildConfigUsesSupportedToolSettings(t *testing.T) {
	adapter := newGoogleAdapter().(googleAdapter)
	config := adapter.buildConfig(Request{
		Model:     "gemini-3-flash-preview",
		System:    "You are helpful.",
		MaxTokens: 2048,
		Tools: []map[string]any{
			{
				"name":        "read",
				"description": "Read a file.",
				"input_schema": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"path": map[string]any{"type": "string"},
					},
					"required": []string{"path"},
				},
			},
		},
	})

	if len(config.Tools) != 1 || len(config.Tools[0].FunctionDeclarations) != 1 {
		t.Fatalf("unexpected config: %#v", config)
	}
	if config.Tools[0].FunctionDeclarations[0].Name != "read" {
		t.Fatalf("unexpected config: %#v", config)
	}
	if config.ToolConfig != nil {
		t.Fatalf("unexpected tool config: %#v", config)
	}
}

func TestGoogleHTTPOptionsUseMillisecondTimeout(t *testing.T) {
	options := googleHTTPOptions("")
	if options.APIVersion != "v1beta" {
		t.Fatalf("unexpected options: %#v", options)
	}
	if options.Timeout == nil || *options.Timeout != googleRequestTimeout {
		t.Fatalf("unexpected timeout: %#v", options)
	}
}

func TestGoogleBuildConfigOmitsUnsupportedExtrasProvider(t *testing.T) {
	adapter := newGoogleAdapter().(googleAdapter)
	config := adapter.buildConfig(Request{
		Model:     "gemini-3-flash-preview",
		System:    "You are helpful.",
		MaxTokens: 2048,
		Tools: []map[string]any{
			{"name": "read", "description": "Read a file.", "input_schema": map[string]any{"type": "object"}},
		},
		ReasoningEffort: "none",
	})
	if config.HTTPOptions == nil {
		t.Fatalf("missing http options: %#v", config)
	}
	if config.HTTPOptions.ExtrasRequestProvider != nil {
		t.Fatalf("unexpected extras provider: %#v", config)
	}
}

func TestGoogleNativePartRestoresSDKShape(t *testing.T) {
	block := message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, map[string]any{
		"native": map[string]any{
			"part": map[string]any{
				"function_call":     map[string]any{"id": "call_1", "name": "read", "args": map[string]any{"path": "x.py"}},
				"thought_signature": "c2ln",
			},
		},
	})

	part := googleNativePart(block)
	if part == nil || part.FunctionCall == nil {
		t.Fatalf("unexpected part: %#v", part)
	}
	if part.FunctionCall.ID != "call_1" || part.FunctionCall.Name != "read" {
		t.Fatalf("unexpected part: %#v", part)
	}
	if string(part.ThoughtSignature) != "sig" {
		t.Fatalf("unexpected part: %#v", part)
	}
	if !reflect.DeepEqual(part.FunctionCall.Args, map[string]any{"path": "x.py"}) {
		t.Fatalf("unexpected part: %#v", part)
	}
}

func TestGoogleHTTPOptionsDropsVersionWhenBaseIncludesAPIVersion(t *testing.T) {
	cases := []string{
		"https://example.test/v1",
		"https://example.test/v1beta",
	}
	for _, baseURL := range cases {
		t.Run(baseURL, func(t *testing.T) {
			options := googleHTTPOptions(baseURL)
			if options.APIVersion != "" {
				t.Fatalf("unexpected options: %#v", options)
			}
			if options.BaseURL != baseURL {
				t.Fatalf("unexpected options: %#v", options)
			}
		})
	}
}

func TestGoogleStreamingPartsMergeIntoFinalBlocks(t *testing.T) {
	adapter := newGoogleAdapter().(googleAdapter)
	blocks := []message.Block{}

	events := adapter.consumePart(&blocks, &genai.Part{Text: "step ", Thought: true})
	if !reflect.DeepEqual(events, []StreamEvent{{Type: "thinking_delta", Text: "step "}}) {
		t.Fatalf("unexpected events: %#v", events)
	}

	events = adapter.consumePart(&blocks, &genai.Part{Text: "one", Thought: true, ThoughtSignature: []byte("sig")})
	if !reflect.DeepEqual(events, []StreamEvent{{Type: "thinking_delta", Text: "one"}}) {
		t.Fatalf("unexpected events: %#v", events)
	}

	part := genai.NewPartFromFunctionCall("read", map[string]any{"path": "x.py"})
	part.FunctionCall.ID = "call_1"
	part.ThoughtSignature = []byte("sig")
	events = adapter.consumePart(&blocks, part)
	if len(events) != 0 {
		t.Fatalf("unexpected events: %#v", events)
	}
	expected := []message.Block{
		message.ThinkingBlock("step one", map[string]any{"native": map[string]any{
			"part": map[string]any{"text": "step one", "thought": true, "thought_signature": "c2ln"},
		}}),
		message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, map[string]any{"native": map[string]any{
			"part": map[string]any{
				"function_call":     map[string]any{"id": "call_1", "args": map[string]any{"path": "x.py"}, "name": "read"},
				"thought_signature": "c2ln",
			},
		}}),
	}
	if !reflect.DeepEqual(blocks, expected) {
		t.Fatalf("unexpected blocks:\n got: %#v\nwant: %#v", blocks, expected)
	}
}

func TestGoogleKeepsSignatureOnlyStreamChunk(t *testing.T) {
	adapter := newGoogleAdapter().(googleAdapter)
	blocks := []message.Block{}

	events := adapter.consumePart(&blocks, &genai.Part{Text: "", ThoughtSignature: []byte("sig")})
	if len(events) != 0 {
		t.Fatalf("unexpected events: %#v", events)
	}
	expected := []message.Block{
		message.TextBlock("", map[string]any{"native": map[string]any{
			"part": map[string]any{"thought_signature": "c2ln"},
		}}),
	}
	if !reflect.DeepEqual(blocks, expected) {
		t.Fatalf("unexpected blocks: %#v", blocks)
	}
}

func containsAnyString(value any, target string) bool {
	items, ok := value.([]any)
	if !ok {
		return false
	}
	for _, item := range items {
		if text, ok := item.(string); ok && text == target {
			return true
		}
	}
	return false
}

func mustBase64Decode(value string) []byte {
	data, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		panic(err)
	}
	return data
}

func mustUnmarshalResponse(t *testing.T, raw string) responses.Response {
	t.Helper()
	var response responses.Response
	if err := json.Unmarshal([]byte(raw), &response); err != nil {
		t.Fatal(err)
	}
	return response
}

func mustUnmarshalOutputItems(t *testing.T, raw string) []responses.ResponseOutputItemUnion {
	t.Helper()
	var items []responses.ResponseOutputItemUnion
	if err := json.Unmarshal([]byte(raw), &items); err != nil {
		t.Fatal(err)
	}
	return items
}
