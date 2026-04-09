package provider

import (
	"context"
	"crypto/sha1"
	"encoding/json"
	"fmt"
	"strings"

	anthropic "github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
	aparam "github.com/anthropics/anthropic-sdk-go/packages/param"
	"github.com/legibet/mycode-go/internal/message"
)

var anthropicThinkingBudgets = map[string]int{
	"low":    2048,
	"medium": 8192,
	"high":   24576,
	"xhigh":  32768,
}

type anthropicAdapter struct {
	baseAdapter
}

func newAnthropicAdapter(id string) Adapter {
	spec, _ := LookupSpec(id)
	return anthropicAdapter{baseAdapter: baseAdapter{spec: spec}}
}

func (a anthropicAdapter) StreamTurn(ctx context.Context, req Request) <-chan StreamEvent {
	out := make(chan StreamEvent, 32)
	go func() {
		defer close(out)

		opts := []option.RequestOption{option.WithAPIKey(req.APIKey)}
		if strings.TrimSpace(req.APIBase) != "" {
			opts = append(opts, option.WithBaseURL(strings.TrimRight(req.APIBase, "/")))
		}
		client := anthropic.NewClient(opts...)

		bodyBytes, err := json.Marshal(a.buildPayload(req))
		if err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}

		stream := client.Messages.NewStreaming(ctx, aparam.Override[anthropic.MessageNewParams](json.RawMessage(bodyBytes)))
		acc := anthropic.Message{}
		for stream.Next() {
			event := stream.Current()
			if err := acc.Accumulate(event); err != nil {
				out <- StreamEvent{Type: "provider_error", Err: err}
				return
			}
			switch variant := event.AsAny().(type) {
			case anthropic.ContentBlockDeltaEvent:
				switch delta := variant.Delta.AsAny().(type) {
				case anthropic.ThinkingDelta:
					if delta.Thinking != "" {
						out <- StreamEvent{Type: "thinking_delta", Text: delta.Thinking}
					}
				case anthropic.TextDelta:
					if delta.Text != "" {
						out <- StreamEvent{Type: "text_delta", Text: delta.Text}
					}
				}
			}
		}
		if err := stream.Err(); err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}

		final := a.convertMessage(acc)
		out <- StreamEvent{Type: "message_done", Msg: &final}
	}()
	return out
}

func (a anthropicAdapter) buildPayload(req Request) map[string]any {
	prepared := prepareMessages(req, a.projectToolCallID)
	messages := make([]map[string]any, 0, len(prepared))
	for _, msg := range prepared {
		messages = append(messages, a.serializeMessage(msg))
	}
	a.applyCacheControl(messages)

	payload := map[string]any{
		"model":      req.Model,
		"max_tokens": req.MaxTokens,
		"messages":   messages,
	}
	if strings.TrimSpace(req.System) != "" {
		payload["system"] = []map[string]any{{
			"type":          "text",
			"text":          req.System,
			"cache_control": map[string]any{"type": "ephemeral"},
		}}
	}
	if len(req.Tools) > 0 {
		tools := make([]map[string]any, 0, len(req.Tools))
		for _, tool := range req.Tools {
			tools = append(tools, map[string]any{
				"name":         tool["name"],
				"description":  tool["description"],
				"input_schema": tool["input_schema"],
			})
		}
		payload["tools"] = tools
		payload["tool_choice"] = map[string]any{"type": "auto"}
	}
	if thinking := a.thinkingConfig(req); thinking != nil {
		payload["thinking"] = thinking
	}
	if outputConfig := a.outputConfig(req); outputConfig != nil {
		payload["output_config"] = outputConfig
	}
	return payload
}

func (a anthropicAdapter) serializeMessage(msg message.Message) map[string]any {
	blocks := make([]map[string]any, 0, len(msg.Content))
	for _, block := range msg.Content {
		blocks = append(blocks, a.serializeBlock(block))
	}
	return map[string]any{
		"role":    msg.Role,
		"content": blocks,
	}
}

func (a anthropicAdapter) serializeBlock(block message.Block) map[string]any {
	switch block.Type {
	case "text":
		return map[string]any{"type": "text", "text": block.Text}
	case "thinking":
		payload := map[string]any{"type": "thinking", "thinking": block.Text}
		if signature, _ := blockNativeMeta(block)["signature"].(string); signature != "" {
			payload["signature"] = signature
		}
		return payload
	case "tool_use":
		payload := map[string]any{
			"type":  "tool_use",
			"id":    block.ID,
			"name":  block.Name,
			"input": defaultInput(block.Input),
		}
		if caller := blockNativeMeta(block)["caller"]; caller != nil {
			payload["caller"] = caller
		}
		return payload
	case "image":
		mimeType, data := loadImageBlockPayload(block)
		return map[string]any{
			"type": "image",
			"source": map[string]any{
				"type":       "base64",
				"media_type": mimeType,
				"data":       data,
			},
		}
	case "document":
		mimeType, data, _ := loadDocumentBlockPayload(block)
		return map[string]any{
			"type": "document",
			"source": map[string]any{
				"type":       "base64",
				"media_type": mimeType,
				"data":       data,
			},
		}
	case "tool_result":
		contentBlocks := make([]map[string]any, 0)
		for _, child := range ToolResultContentBlocks(block) {
			switch child.Type {
			case "text":
				contentBlocks = append(contentBlocks, map[string]any{"type": "text", "text": child.Text})
			case "image":
				mimeType, data := loadImageBlockPayload(child)
				contentBlocks = append(contentBlocks, map[string]any{
					"type": "image",
					"source": map[string]any{
						"type":       "base64",
						"media_type": mimeType,
						"data":       data,
					},
				})
			}
		}
		content := any(block.ModelText)
		if len(contentBlocks) > 0 {
			content = contentBlocks
		}
		return map[string]any{
			"type":        "tool_result",
			"tool_use_id": block.ToolUseID,
			"content":     content,
			"is_error":    block.IsError != nil && *block.IsError,
		}
	default:
		return dumpJSONMap(block)
	}
}

func (a anthropicAdapter) thinkingConfig(req Request) map[string]any {
	switch a.Spec().ID {
	case "anthropic":
		effort := req.ReasoningEffort
		if effort == "" {
			return nil
		}
		if effort == "none" {
			return map[string]any{"type": "disabled"}
		}
		model := strings.ToLower(req.Model)
		if strings.HasPrefix(model, "claude-sonnet-4-6") || strings.HasPrefix(model, "claude-opus-4-6") {
			return map[string]any{"type": "adaptive"}
		}
		return manualAnthropicThinkingConfig(effort)
	case "moonshotai", "minimax":
		return manualAnthropicThinkingConfig(req.ReasoningEffort)
	default:
		return nil
	}
}

func (a anthropicAdapter) outputConfig(req Request) map[string]any {
	if a.Spec().ID != "anthropic" || req.ReasoningEffort == "" || req.ReasoningEffort == "none" {
		return nil
	}
	model := strings.ToLower(req.Model)
	switch {
	case strings.HasPrefix(model, "claude-sonnet-4-6"):
		effort := req.ReasoningEffort
		if effort == "xhigh" {
			effort = "high"
		}
		return map[string]any{"effort": effort}
	case strings.HasPrefix(model, "claude-opus-4-6"):
		effort := req.ReasoningEffort
		if effort == "xhigh" {
			effort = "max"
		}
		return map[string]any{"effort": effort}
	default:
		return nil
	}
}

func (a anthropicAdapter) projectToolCallID(toolCallID string, used map[string]struct{}) string {
	safe := strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z':
			return r
		case r >= 'A' && r <= 'Z':
			return r
		case r >= '0' && r <= '9':
			return r
		case r == '_' || r == '-':
			return r
		default:
			return '_'
		}
	}, toolCallID)
	if safe == toolCallID && len(safe) <= 64 {
		if _, ok := used[safe]; !ok {
			return safe
		}
	}
	prefix := safe
	if prefix == "" {
		prefix = "tool"
	}
	if len(prefix) > 55 {
		prefix = prefix[:55]
	}
	digest := fmt.Sprintf("%x", sha1.Sum([]byte(toolCallID)))[:8]
	candidate := prefix + "_" + digest
	if _, ok := used[candidate]; !ok {
		return candidate
	}
	for index := 2; ; index++ {
		suffix := fmt.Sprintf("_%s_%d", digest, index)
		trimmed := prefix
		if len(trimmed)+len(suffix) > 64 {
			trimmed = trimmed[:64-len(suffix)]
		}
		candidate = trimmed + suffix
		if _, ok := used[candidate]; !ok {
			return candidate
		}
	}
}

func (a anthropicAdapter) applyCacheControl(messages []map[string]any) {
	for index := len(messages) - 1; index >= 0; index-- {
		msg := messages[index]
		role, _ := msg["role"].(string)
		if role != "user" {
			continue
		}
		content, _ := msg["content"].([]map[string]any)
		if content == nil {
			rawList, _ := msg["content"].([]any)
			content = make([]map[string]any, 0, len(rawList))
			for _, item := range rawList {
				if block, ok := item.(map[string]any); ok {
					content = append(content, block)
				}
			}
		}
		for i := len(content) - 1; i >= 0; i-- {
			blockType, _ := content[i]["type"].(string)
			if blockType != "text" && blockType != "image" && blockType != "document" && blockType != "tool_result" {
				continue
			}
			content[i]["cache_control"] = map[string]any{"type": "ephemeral"}
			return
		}
		return
	}
}

func (a anthropicAdapter) convertMessage(msg anthropic.Message) message.Message {
	blocks := make([]message.Block, 0, len(msg.Content))
	for _, item := range msg.Content {
		switch block := item.AsAny().(type) {
		case anthropic.ThinkingBlock:
			meta := map[string]any{}
			if block.Signature != "" {
				meta["native"] = map[string]any{"signature": block.Signature}
			}
			blocks = append(blocks, message.ThinkingBlock(block.Thinking, meta))
		case anthropic.TextBlock:
			meta := map[string]any{}
			if citations := dumpJSON(block.Citations); citations != nil {
				meta["native"] = map[string]any{"citations": citations}
			}
			blocks = append(blocks, message.TextBlock(block.Text, meta))
		case anthropic.ToolUseBlock:
			input := map[string]any{}
			if len(block.Input) > 0 {
				_ = json.Unmarshal(block.Input, &input)
			}
			meta := map[string]any{}
			if caller := dumpJSON(block.Caller); caller != nil {
				meta["native"] = map[string]any{"caller": caller}
			}
			blocks = append(blocks, message.ToolUseBlock(block.ID, block.Name, input, meta))
		}
	}
	nativeMeta := map[string]any{}
	if msg.StopSequence != "" {
		nativeMeta["stop_sequence"] = msg.StopSequence
	}
	if msg.Usage.ServiceTier != "" {
		nativeMeta["service_tier"] = string(msg.Usage.ServiceTier)
	}
	return message.AssistantMessage(
		blocks,
		a.Spec().ID,
		msg.Model,
		msg.ID,
		string(msg.StopReason),
		dumpJSON(msg.Usage),
		nativeMeta,
	)
}

func manualAnthropicThinkingConfig(effort string) map[string]any {
	if effort == "" {
		return nil
	}
	if effort == "none" {
		return map[string]any{"type": "disabled"}
	}
	budget := anthropicThinkingBudgets[effort]
	if budget == 0 {
		return nil
	}
	return map[string]any{"type": "enabled", "budget_tokens": budget}
}

func defaultInput(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	return input
}
