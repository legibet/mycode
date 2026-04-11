package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"google.golang.org/genai"

	"github.com/legibet/mycode-go/internal/message"
)

const googleDummyThoughtSignature = "skip_thought_signature_validator"
const googleRequestTimeout = 300 * time.Second

type googleAdapter struct {
	baseAdapter
}

func newGoogleAdapter() Adapter {
	spec, _ := LookupSpec("google")
	return googleAdapter{baseAdapter: baseAdapter{spec: spec}}
}

func (a googleAdapter) StreamTurn(ctx context.Context, req Request) <-chan StreamEvent {
	out := make(chan StreamEvent, 32)
	go func() {
		defer close(out)
		requestCtx := ctx
		cancel := func() {}
		// Keep the same 300s effective timeout as the Python adapter, but apply
		// it on our own context. The current Go genai streaming path cancels a
		// request-scoped timeout too early if it is passed via HTTPOptions.
		if googleRequestTimeout > 0 {
			if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > googleRequestTimeout {
				requestCtx, cancel = context.WithTimeout(ctx, googleRequestTimeout)
			}
		}
		defer cancel()

		client, err := genai.NewClient(requestCtx, &genai.ClientConfig{
			APIKey:      req.APIKey,
			Backend:     genai.BackendGeminiAPI,
			HTTPOptions: *googleHTTPOptions(req.APIBase),
		})
		if err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}

		config := a.buildConfig(req)
		blocks := make([]message.Block, 0)
		responseID := ""
		responseModel := ""
		finishReason := ""
		finishMessage := ""
		var usage any

		for response, err := range client.Models.GenerateContentStream(requestCtx, req.Model, a.buildContents(req), config) {
			if err != nil {
				out <- StreamEvent{Type: "provider_error", Err: err}
				return
			}
			if response == nil {
				continue
			}
			if responseID == "" {
				responseID = response.ResponseID
			}
			if responseModel == "" {
				responseModel = response.ModelVersion
			}
			if dumped := dumpJSON(response.UsageMetadata); dumped != nil {
				usage = dumped
			}
			if len(response.Candidates) == 0 || response.Candidates[0] == nil {
				continue
			}
			candidate := response.Candidates[0]
			if candidate.FinishReason != "" {
				finishReason = string(candidate.FinishReason)
			}
			if candidate.FinishMessage != "" {
				finishMessage = candidate.FinishMessage
			}
			if candidate.Content == nil {
				continue
			}
			for _, part := range candidate.Content.Parts {
				for _, event := range a.consumePart(&blocks, part) {
					out <- event
				}
			}
		}

		nativeMeta := map[string]any{}
		if finishMessage != "" {
			nativeMeta["finish_message"] = finishMessage
		}
		msg := message.AssistantMessage(blocks, a.Spec().ID, defaultString(responseModel, req.Model), responseID, finishReason, usage, nativeMeta)
		out <- StreamEvent{Type: "message_done", Msg: &msg}
	}()
	return out
}

func (a googleAdapter) buildContents(req Request) []*genai.Content {
	contents := make([]*genai.Content, 0)
	toolNames := map[string]string{}
	for _, msg := range prepareMessages(req, defaultProjectToolCallID) {
		switch msg.Role {
		case "assistant":
			parts := make([]*genai.Part, 0, len(msg.Content))
			needsDummySignature := true
			for _, block := range msg.Content {
				if block.Type == "tool_use" && block.ID != "" && block.Name != "" {
					toolNames[block.ID] = block.Name
				}
				if native := googleNativePart(block); native != nil {
					parts = append(parts, native)
					if native.FunctionCall != nil && len(native.ThoughtSignature) > 0 {
						needsDummySignature = false
					}
					continue
				}
				switch block.Type {
				case "thinking":
					parts = append(parts, &genai.Part{Text: block.Text, Thought: true})
				case "text":
					parts = append(parts, genai.NewPartFromText(block.Text))
				case "tool_use":
					part := genai.NewPartFromFunctionCall(block.Name, defaultInput(block.Input))
					part.FunctionCall.ID = block.ID
					if needsDummySignature {
						part.ThoughtSignature = []byte(googleDummyThoughtSignature)
						needsDummySignature = false
					}
					parts = append(parts, part)
				}
			}
			if len(parts) > 0 {
				contents = append(contents, genai.NewContentFromParts(parts, genai.RoleModel))
			}
		case "user":
			parts := make([]*genai.Part, 0, len(msg.Content))
			for _, block := range msg.Content {
				switch block.Type {
				case "text":
					parts = append(parts, genai.NewPartFromText(block.Text))
				case "image":
					mimeType, data := loadImageBlockPayload(block)
					parts = append(parts, genai.NewPartFromBytes(decodeBase64(data), mimeType))
				case "document":
					mimeType, data, _ := loadDocumentBlockPayload(block)
					parts = append(parts, genai.NewPartFromBytes(decodeBase64(data), mimeType))
				case "tool_result":
					response := map[string]any{"result": block.ModelText}
					if block.IsError != nil && *block.IsError {
						response["is_error"] = true
					}
					part := genai.NewPartFromFunctionResponse(toolNames[block.ToolUseID], response)
					part.FunctionResponse.ID = block.ToolUseID
					parts = append(parts, part)
				}
			}
			if len(parts) > 0 {
				contents = append(contents, genai.NewContentFromParts(parts, genai.RoleUser))
			}
		}
	}
	return contents
}

func (a googleAdapter) buildConfig(req Request) *genai.GenerateContentConfig {
	config := &genai.GenerateContentConfig{
		HTTPOptions:       googleHTTPOptions(req.APIBase),
		MaxOutputTokens:   int32(req.MaxTokens),
		ThinkingConfig:    &genai.ThinkingConfig{IncludeThoughts: true},
		SystemInstruction: genai.NewContentFromText(req.System, genai.RoleUser),
	}
	if level := googleThinkingLevel(req.Model, req.ReasoningEffort); level != genai.ThinkingLevelUnspecified {
		config.ThinkingConfig.ThinkingLevel = level
	}
	if strings.TrimSpace(req.System) == "" {
		config.SystemInstruction = nil
	}
	if len(req.Tools) > 0 {
		declarations := make([]*genai.FunctionDeclaration, 0, len(req.Tools))
		for _, tool := range req.Tools {
			declarations = append(declarations, &genai.FunctionDeclaration{
				Name:                 fmt.Sprintf("%v", tool["name"]),
				Description:          fmt.Sprintf("%v", tool["description"]),
				ParametersJsonSchema: tool["input_schema"],
			})
		}
		config.Tools = []*genai.Tool{{
			FunctionDeclarations: declarations,
		}}
	}
	return config
}

func googleHTTPOptions(apiBase string) *genai.HTTPOptions {
	return &genai.HTTPOptions{
		BaseURL:    strings.TrimSpace(apiBase),
		APIVersion: googleAPIVersion(apiBase),
	}
}

func (a googleAdapter) consumePart(blocks *[]message.Block, part *genai.Part) []StreamEvent {
	if part == nil {
		return nil
	}
	nativePart := dumpJSONMap(part)
	if thought, ok := nativePart["thought"].(bool); ok && !thought {
		delete(nativePart, "thought")
	}
	geminiPartCamelToSnake(nativePart)

	if part.FunctionCall != nil {
		id := part.FunctionCall.ID
		if id == "" {
			id = fmt.Sprintf("tool_call_%d", len(*blocks))
		}
		*blocks = append(*blocks, message.ToolUseBlock(
			id,
			part.FunctionCall.Name,
			part.FunctionCall.Args,
			map[string]any{"native": map[string]any{"part": nativePart}},
		))
		return nil
	}

	if part.Text == "" {
		if len(part.ThoughtSignature) == 0 {
			return nil
		}
		meta := map[string]any{"native": map[string]any{"part": nativePart}}
		if part.Thought {
			*blocks = append(*blocks, message.ThinkingBlock("", meta))
		} else {
			*blocks = append(*blocks, message.TextBlock("", meta))
		}
		return nil
	}

	blockType := "text"
	eventType := "text_delta"
	if part.Thought {
		blockType = "thinking"
		eventType = "thinking_delta"
	}
	event := StreamEvent{Type: eventType, Text: part.Text}

	if len(*blocks) > 0 && (*blocks)[len(*blocks)-1].Type == blockType {
		last := &(*blocks)[len(*blocks)-1]
		lastPart, _ := blockNativeMeta(*last)["part"].(map[string]any)
		lastSignature, _ := lastPart["thought_signature"].(string)
		currentSignature, _ := nativePart["thought_signature"].(string)
		if lastSignature == "" || currentSignature == "" || lastSignature == currentSignature {
			last.Text += part.Text
			if lastPart != nil {
				lastText, _ := lastPart["text"].(string)
				lastPart["text"] = lastText + part.Text
				if currentSignature != "" && lastSignature == "" {
					lastPart["thought_signature"] = currentSignature
				}
			}
			return []StreamEvent{event}
		}
	}

	meta := map[string]any{"native": map[string]any{"part": nativePart}}
	if part.Thought {
		*blocks = append(*blocks, message.ThinkingBlock(part.Text, meta))
	} else {
		*blocks = append(*blocks, message.TextBlock(part.Text, meta))
	}
	return []StreamEvent{event}
}

func googleNativePart(block message.Block) *genai.Part {
	rawPart, _ := blockNativeMeta(block)["part"].(map[string]any)
	if len(rawPart) == 0 {
		return nil
	}
	data, err := json.Marshal(geminiPartSnakeToCamel(rawPart))
	if err != nil {
		return nil
	}
	var part genai.Part
	if err := json.Unmarshal(data, &part); err != nil {
		return nil
	}
	return &part
}

// geminiPartCamelToSnake renames the camelCase keys that the Go genai SDK
// emits via json.Marshal to the snake_case keys used by the Python backend.
// This keeps session files compatible across both runtimes.
// Add a new entry here whenever the Go SDK introduces a new camelCase field
// that needs cross-backend replay support.
var geminiPartCamelKeys = [][2]string{
	{"functionCall", "function_call"},
	{"thoughtSignature", "thought_signature"},
}

func geminiPartCamelToSnake(p map[string]any) {
	for _, pair := range geminiPartCamelKeys {
		if v, ok := p[pair[0]]; ok {
			p[pair[1]] = v
			delete(p, pair[0])
		}
	}
}

// geminiPartSnakeToCamel is the inverse of geminiPartCamelToSnake.
// Used before json.Unmarshal into genai.Part, which requires camelCase tags.
func geminiPartSnakeToCamel(p map[string]any) map[string]any {
	out := make(map[string]any, len(p))
	for k, v := range p {
		out[k] = v
	}
	for _, pair := range geminiPartCamelKeys {
		if v, ok := out[pair[1]]; ok {
			out[pair[0]] = v
			delete(out, pair[1])
		}
	}
	return out
}

func googleAPIVersion(apiBase string) string {
	base := strings.TrimRight(strings.TrimSpace(apiBase), "/")
	lower := strings.ToLower(base)
	if strings.HasSuffix(lower, "/v1") || strings.HasSuffix(lower, "/v1beta") {
		return ""
	}
	return "v1beta"
}

func googleThinkingLevel(model, effort string) genai.ThinkingLevel {
	if effort == "" {
		return genai.ThinkingLevelUnspecified
	}
	model = strings.ToLower(model)
	if !strings.HasPrefix(model, "gemini-3") {
		return genai.ThinkingLevelUnspecified
	}
	switch effort {
	case "none", "low":
		if strings.HasPrefix(model, "gemini-3.1-pro") {
			return genai.ThinkingLevelLow
		}
		return genai.ThinkingLevelMinimal
	case "medium":
		return genai.ThinkingLevelMedium
	default:
		return genai.ThinkingLevelHigh
	}
}
