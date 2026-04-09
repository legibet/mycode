package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/tools"
	openai "github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	oparam "github.com/openai/openai-go/v3/packages/param"
)

type chatToolCallState struct {
	Index         int
	ToolID        string
	Name          string
	ArgumentsText string
}

type openAIChatAdapter struct {
	baseAdapter
}

func newOpenAIChatAdapter(id string) Adapter {
	spec, _ := LookupSpec(id)
	return openAIChatAdapter{baseAdapter: baseAdapter{spec: spec}}
}

func (a openAIChatAdapter) StreamTurn(ctx context.Context, req Request) <-chan StreamEvent {
	out := make(chan StreamEvent, 32)
	go func() {
		defer close(out)

		opts := []option.RequestOption{option.WithAPIKey(req.APIKey)}
		if strings.TrimSpace(req.APIBase) != "" {
			opts = append(opts, option.WithBaseURL(strings.TrimRight(req.APIBase, "/")))
		}
		client := openai.NewClient(opts...)

		bodyBytes, err := json.Marshal(a.buildPayload(req))
		if err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}

		stream := client.Chat.Completions.NewStreaming(ctx, oparam.Override[openai.ChatCompletionNewParams](json.RawMessage(bodyBytes)))
		toolCalls := map[int]*chatToolCallState{}
		textParts := make([]string, 0)
		thinkingParts := make([]string, 0)
		thinkingMeta := map[string]any{}
		responseID := ""
		responseModel := ""
		finishReason := ""
		var usage any

		for stream.Next() {
			chunk := stream.Current()
			if responseID == "" {
				responseID = chunk.ID
			}
			if responseModel == "" {
				responseModel = chunk.Model
			}
			if dumped := dumpJSON(chunk.Usage); dumped != nil {
				usage = dumped
			}
			if len(chunk.Choices) == 0 {
				continue
			}

			choice := chunk.Choices[0]
			if choice.FinishReason != "" {
				finishReason = choice.FinishReason
			}

			delta := choice.Delta
			reasoningDelta, metaUpdate := extractChatReasoningDelta(delta.RawJSON())
			if reasoningDelta != "" {
				thinkingParts = append(thinkingParts, reasoningDelta)
				for key, value := range metaUpdate {
					thinkingMeta[key] = value
				}
				out <- StreamEvent{Type: "thinking_delta", Text: reasoningDelta}
			}

			if delta.Content != "" {
				textParts = append(textParts, delta.Content)
				out <- StreamEvent{Type: "text_delta", Text: delta.Content}
			}

			for _, toolCall := range delta.ToolCalls {
				index := int(toolCall.Index)
				state := toolCalls[index]
				if state == nil {
					state = &chatToolCallState{Index: index}
					toolCalls[index] = state
				}
				if toolCall.ID != "" {
					state.ToolID = toolCall.ID
				}
				if toolCall.Function.Name != "" {
					state.Name = toolCall.Function.Name
				}
				if toolCall.Function.Arguments != "" {
					state.ArgumentsText += toolCall.Function.Arguments
				}
			}
		}
		if err := stream.Err(); err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}

		blocks := make([]message.Block, 0, len(toolCalls)+2)
		if len(thinkingParts) > 0 {
			meta := map[string]any{}
			if len(thinkingMeta) > 0 {
				meta["native"] = thinkingMeta
			}
			blocks = append(blocks, message.ThinkingBlock(strings.Join(thinkingParts, ""), meta))
		}
		if len(textParts) > 0 {
			blocks = append(blocks, message.TextBlock(strings.Join(textParts, ""), nil))
		}
		for index := 0; index < len(toolCalls); index++ {
			state := toolCalls[index]
			if state == nil {
				continue
			}
			toolInput, err := tools.ParseToolArguments(state.ArgumentsText)
			meta := map[string]any{}
			if err != nil {
				meta["native"] = map[string]any{"raw_arguments": state.ArgumentsText}
				toolInput = map[string]any{}
			}
			blocks = append(blocks, message.ToolUseBlock(defaultString(state.ToolID, fmt.Sprintf("tool_call_%d", index)), state.Name, toolInput, meta))
		}

		msg := message.AssistantMessage(blocks, a.Spec().ID, responseModel, responseID, finishReason, usage, nil)
		out <- StreamEvent{Type: "message_done", Msg: &msg}
	}()
	return out
}

func (a openAIChatAdapter) buildPayload(req Request) map[string]any {
	messagesPayload := make([]any, 0)
	if strings.TrimSpace(req.System) != "" {
		messagesPayload = append(messagesPayload, map[string]any{
			"role":    "system",
			"content": req.System,
		})
	}
	for _, msg := range prepareMessages(req, defaultProjectToolCallID) {
		messagesPayload = append(messagesPayload, a.serializeMessage(msg)...)
	}

	payload := map[string]any{
		"model":          req.Model,
		"messages":       messagesPayload,
		"max_tokens":     req.MaxTokens,
		"stream_options": map[string]any{"include_usage": true},
	}
	if len(req.Tools) > 0 {
		toolsPayload := make([]any, 0, len(req.Tools))
		for _, tool := range req.Tools {
			toolsPayload = append(toolsPayload, map[string]any{
				"type": "function",
				"function": map[string]any{
					"name":        tool["name"],
					"description": tool["description"],
					"parameters":  tool["input_schema"],
				},
			})
		}
		payload["tools"] = toolsPayload
		payload["tool_choice"] = "auto"
	}

	switch a.Spec().ID {
	case "zai":
		payload["extra_body"] = map[string]any{
			"thinking": map[string]any{
				"type":           "enabled",
				"clear_thinking": false,
			},
		}
	case "openrouter":
		if req.ReasoningEffort != "" {
			payload["extra_body"] = map[string]any{
				"reasoning": map[string]any{"effort": req.ReasoningEffort},
			}
		}
	}
	return payload
}

func (a openAIChatAdapter) serializeMessage(msg message.Message) []any {
	role := msg.Role
	switch role {
	case "user":
		payloadMessages := make([]any, 0)
		hasMedia := false
		for _, block := range msg.Content {
			if block.Type == "image" || block.Type == "document" {
				hasMedia = true
				break
			}
		}

		if hasMedia {
			content := make([]any, 0)
			for _, block := range msg.Content {
				switch block.Type {
				case "text":
					if block.Text != "" {
						content = append(content, map[string]any{"type": "text", "text": block.Text})
					}
				case "image":
					mimeType, data := loadImageBlockPayload(block)
					content = append(content, map[string]any{
						"type": "image_url",
						"image_url": map[string]any{
							"url": "data:" + mimeType + ";base64," + data,
						},
					})
				case "document":
					mimeType, data, name := loadDocumentBlockPayload(block)
					content = append(content, map[string]any{
						"type": "file",
						"file": map[string]any{
							"filename":  name,
							"file_data": "data:" + mimeType + ";base64," + data,
						},
					})
				}
			}
			if len(content) > 0 {
				payloadMessages = append(payloadMessages, map[string]any{
					"role":    "user",
					"content": content,
				})
			}
		} else {
			textParts := make([]string, 0)
			for _, block := range msg.Content {
				if block.Type == "text" && block.Text != "" {
					textParts = append(textParts, block.Text)
				}
			}
			if len(textParts) > 0 {
				payloadMessages = append(payloadMessages, map[string]any{
					"role":    "user",
					"content": strings.Join(textParts, "\n"),
				})
			}
		}

		for _, block := range msg.Content {
			if block.Type != "tool_result" {
				continue
			}
			payloadMessages = append(payloadMessages, map[string]any{
				"role":         "tool",
				"tool_call_id": block.ToolUseID,
				"content":      block.ModelText,
			})
		}
		return payloadMessages
	case "assistant":
		textParts := make([]string, 0)
		thinkingBlocks := make([]message.Block, 0)
		toolUseBlocks := make([]message.Block, 0)
		for _, block := range msg.Content {
			switch block.Type {
			case "text":
				if block.Text != "" {
					textParts = append(textParts, block.Text)
				}
			case "thinking":
				thinkingBlocks = append(thinkingBlocks, block)
			case "tool_use":
				toolUseBlocks = append(toolUseBlocks, block)
			}
		}

		payload := map[string]any{"role": "assistant"}
		if len(textParts) > 0 {
			payload["content"] = strings.Join(textParts, "\n")
		}
		if len(toolUseBlocks) > 0 {
			toolCalls := make([]any, 0, len(toolUseBlocks))
			for _, block := range toolUseBlocks {
				toolCalls = append(toolCalls, map[string]any{
					"id":   block.ID,
					"type": "function",
					"function": map[string]any{
						"name":      block.Name,
						"arguments": mustJSON(defaultInput(block.Input)),
					},
				})
			}
			payload["tool_calls"] = toolCalls
		}
		if len(thinkingBlocks) > 0 {
			for key, value := range serializeChatReasoning(thinkingBlocks) {
				payload[key] = value
			}
		}
		return []any{payload}
	default:
		return nil
	}
}

func extractChatReasoningDelta(rawJSON string) (string, map[string]any) {
	raw := unmarshalJSONMap(rawJSON)
	if len(raw) == 0 {
		return "", nil
	}
	modelExtra, _ := raw["model_extra"].(map[string]any)
	for _, source := range []map[string]any{raw, modelExtra} {
		if len(source) == 0 {
			continue
		}
		if reasoningContent, _ := source["reasoning_content"].(string); reasoningContent != "" {
			return reasoningContent, map[string]any{"reasoning_field": "reasoning_content"}
		}
		if reasoningDetails, ok := source["reasoning_details"].([]any); ok && len(reasoningDetails) > 0 {
			textParts := make([]string, 0, len(reasoningDetails))
			for _, item := range reasoningDetails {
				if entry, ok := item.(map[string]any); ok {
					if text, _ := entry["text"].(string); text != "" {
						textParts = append(textParts, text)
					}
				}
			}
			if len(textParts) > 0 {
				return strings.Join(textParts, ""), map[string]any{
					"reasoning_field":   "reasoning_details",
					"reasoning_details": reasoningDetails,
				}
			}
		}
	}
	return "", nil
}

func serializeChatReasoning(blocks []message.Block) map[string]any {
	thinkingText := make([]string, 0, len(blocks))
	for _, block := range blocks {
		if block.Text != "" {
			thinkingText = append(thinkingText, block.Text)
		}
	}
	native := blockNativeMeta(blocks[0])
	if field, _ := native["reasoning_field"].(string); field == "reasoning_details" {
		if details := native["reasoning_details"]; details != nil {
			return map[string]any{"reasoning_details": details}
		}
	}
	if len(thinkingText) > 0 {
		return map[string]any{"reasoning_content": strings.Join(thinkingText, "\n")}
	}
	return nil
}
