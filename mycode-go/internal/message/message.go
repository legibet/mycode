package message

import (
	"maps"
	"slices"
	"strings"
)

// Block is the canonical content block persisted in sessions and used in API responses.
type Block struct {
	Type        string         `json:"type"`
	Text        string         `json:"text,omitempty"`
	Data        string         `json:"data,omitempty"`
	MIMEType    string         `json:"mime_type,omitempty"`
	Name        string         `json:"name,omitempty"`
	ID          string         `json:"id,omitempty"`
	Input       map[string]any `json:"input,omitempty"`
	ToolUseID   string         `json:"tool_use_id,omitempty"`
	ModelText   string         `json:"model_text,omitempty"`
	DisplayText string         `json:"display_text,omitempty"`
	IsError     *bool          `json:"is_error,omitempty"`
	Content     []Block        `json:"content,omitempty"`
	Meta        map[string]any `json:"meta,omitempty"`
}

// Message is the single runtime and persistence message format.
type Message struct {
	Role    string         `json:"role"`
	Content []Block        `json:"content,omitempty"`
	Meta    map[string]any `json:"meta,omitempty"`
}

// Bool returns a stable pointer for JSON optional booleans.
func Bool(v bool) *bool {
	return &v
}

// TextBlock returns a plain text block.
func TextBlock(text string, meta map[string]any) Block {
	block := Block{Type: "text", Text: text}
	if len(meta) > 0 {
		block.Meta = maps.Clone(meta)
	}
	return block
}

// ThinkingBlock returns a reasoning block.
func ThinkingBlock(text string, meta map[string]any) Block {
	block := Block{Type: "thinking", Text: text}
	if len(meta) > 0 {
		block.Meta = maps.Clone(meta)
	}
	return block
}

// ImageBlock returns an image block.
func ImageBlock(data, mimeType, name string, meta map[string]any) Block {
	block := Block{Type: "image", Data: data, MIMEType: mimeType, Name: name}
	if len(meta) > 0 {
		block.Meta = maps.Clone(meta)
	}
	return block
}

// DocumentBlock returns a document block.
func DocumentBlock(data, mimeType, name string, meta map[string]any) Block {
	block := Block{Type: "document", Data: data, MIMEType: mimeType, Name: name}
	if len(meta) > 0 {
		block.Meta = maps.Clone(meta)
	}
	return block
}

// ToolUseBlock returns a tool use block.
func ToolUseBlock(id, name string, input map[string]any, meta map[string]any) Block {
	block := Block{
		Type:  "tool_use",
		ID:    id,
		Name:  name,
		Input: maps.Clone(input),
	}
	if len(meta) > 0 {
		block.Meta = maps.Clone(meta)
	}
	return block
}

// ToolResultBlock returns a tool result block.
func ToolResultBlock(toolUseID, modelText, displayText string, isError bool, content []Block, meta map[string]any) Block {
	block := Block{
		Type:        "tool_result",
		ToolUseID:   toolUseID,
		ModelText:   modelText,
		DisplayText: displayText,
		IsError:     Bool(isError),
	}
	if len(content) > 0 {
		block.Content = slices.Clone(content)
	}
	if len(meta) > 0 {
		block.Meta = maps.Clone(meta)
	}
	return block
}

// BuildMessage returns a canonical message.
func BuildMessage(role string, blocks []Block, meta map[string]any) Message {
	msg := Message{Role: role}
	if len(blocks) > 0 {
		msg.Content = slices.Clone(blocks)
	}
	if len(meta) > 0 {
		msg.Meta = maps.Clone(meta)
	}
	return msg
}

// UserTextMessage returns a text-only user message.
func UserTextMessage(text string, meta map[string]any) Message {
	return BuildMessage("user", []Block{TextBlock(text, nil)}, meta)
}

// AssistantMessage returns a normalized assistant message.
func AssistantMessage(blocks []Block, provider, model, providerMessageID, stopReason string, usage any, nativeMeta map[string]any) Message {
	meta := map[string]any{}
	if provider != "" {
		meta["provider"] = provider
	}
	if model != "" {
		meta["model"] = model
	}
	if providerMessageID != "" {
		meta["provider_message_id"] = providerMessageID
	}
	if stopReason != "" {
		meta["stop_reason"] = stopReason
	}
	if usage != nil {
		meta["usage"] = usage
	}
	if len(nativeMeta) > 0 {
		meta["native"] = maps.Clone(nativeMeta)
	}
	return BuildMessage("assistant", blocks, meta)
}

// FlattenText returns readable text while skipping attachment snapshots.
func FlattenText(msg Message, includeThinking bool) string {
	parts := make([]string, 0, len(msg.Content))
	for _, block := range msg.Content {
		if block.Meta != nil && truthy(block.Meta["attachment"]) {
			continue
		}
		if block.Type == "text" || (includeThinking && block.Type == "thinking") {
			text := strings.TrimSpace(block.Text)
			if text != "" {
				parts = append(parts, text)
			}
		}
	}
	return strings.Join(parts, " ")
}

// Clone returns a deep-enough copy for replay and persistence.
func Clone(msg Message) Message {
	out := Message{Role: msg.Role}
	if len(msg.Content) > 0 {
		out.Content = make([]Block, len(msg.Content))
		for i, block := range msg.Content {
			out.Content[i] = CloneBlock(block)
		}
	}
	if len(msg.Meta) > 0 {
		out.Meta = maps.Clone(msg.Meta)
	}
	return out
}

// CloneBlock returns a copy of a block.
func CloneBlock(block Block) Block {
	out := block
	if len(block.Input) > 0 {
		out.Input = maps.Clone(block.Input)
	}
	if len(block.Content) > 0 {
		out.Content = make([]Block, len(block.Content))
		for i, child := range block.Content {
			out.Content[i] = CloneBlock(child)
		}
	}
	if len(block.Meta) > 0 {
		out.Meta = maps.Clone(block.Meta)
	}
	if block.IsError != nil {
		value := *block.IsError
		out.IsError = &value
	}
	return out
}

func truthy(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case string:
		return v == "true" || v == "1"
	default:
		return false
	}
}
