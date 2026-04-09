package provider

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/legibet/mycode-go/internal/message"
)

// Request is one provider turn request.
type Request struct {
	Provider           string
	Model              string
	SessionID          string
	Messages           []message.Message
	System             string
	Tools              []map[string]any
	MaxTokens          int
	APIKey             string
	APIBase            string
	ReasoningEffort    string
	SupportsImageInput bool
	SupportsPDFInput   bool
}

// StreamEvent is the normalized provider stream event.
type StreamEvent struct {
	Type string
	Text string
	Msg  *message.Message
	Err  error
}

// Adapter streams one provider turn.
type Adapter interface {
	Spec() Spec
	StreamTurn(ctx context.Context, req Request) <-chan StreamEvent
}

type baseAdapter struct {
	spec Spec
}

func (b baseAdapter) Spec() Spec {
	return b.spec
}

func prepareMessages(req Request, projectToolCallID func(string, map[string]struct{}) string) []message.Message {
	repaired := RepairMessagesForReplay(req.Messages, req.SupportsImageInput, req.SupportsPDFInput)
	prepared := make([]message.Message, 0, len(repaired))
	toolIDs := map[string]string{}
	usedProjected := map[string]struct{}{}
	for _, msg := range repaired {
		next := message.Clone(msg)
		for i := range next.Content {
			block := next.Content[i]
			switch block.Type {
			case "tool_use":
				if block.ID != "" {
					if _, ok := toolIDs[block.ID]; !ok {
						toolIDs[block.ID] = projectToolCallID(block.ID, usedProjected)
						usedProjected[toolIDs[block.ID]] = struct{}{}
					}
					next.Content[i].ID = toolIDs[block.ID]
				}
			case "tool_result":
				if projected, ok := toolIDs[block.ToolUseID]; ok {
					next.Content[i].ToolUseID = projected
				}
			}
		}
		prepared = append(prepared, next)
	}
	return prepared
}

func defaultProjectToolCallID(toolCallID string, _ map[string]struct{}) string {
	return toolCallID
}

// RepairMessagesForReplay converts canonical history into a replay-safe transcript.
func RepairMessagesForReplay(source []message.Message, supportsImageInput, supportsPDFInput bool) []message.Message {
	replay := make([]message.Message, 0, len(source))
	emittedToolUseIDs := map[string]struct{}{}
	emittedToolResultIDs := map[string]struct{}{}
	openToolUseIDs := []string{}

	for _, msg := range source {
		switch msg.Role {
		case "assistant":
			if len(openToolUseIDs) > 0 {
				replay = append(replay, interruptedToolResultMessage(openToolUseIDs))
				for _, id := range openToolUseIDs {
					emittedToolResultIDs[id] = struct{}{}
				}
				openToolUseIDs = nil
			}
			stopReason, _ := msg.Meta["stop_reason"].(string)
			if stopReason == "error" || stopReason == "aborted" || stopReason == "cancelled" {
				continue
			}
			content := []message.Block{}
			currentToolUseIDs := []string{}
			for _, block := range msg.Content {
				switch block.Type {
				case "text", "thinking":
					if strings.TrimSpace(block.Text) != "" {
						content = append(content, message.CloneBlock(block))
					}
				case "tool_use":
					if block.ID == "" {
						continue
					}
					if _, exists := emittedToolUseIDs[block.ID]; exists {
						continue
					}
					emittedToolUseIDs[block.ID] = struct{}{}
					currentToolUseIDs = append(currentToolUseIDs, block.ID)
					content = append(content, message.CloneBlock(block))
				}
			}
			if len(content) == 0 {
				continue
			}
			next := message.Clone(msg)
			next.Content = content
			replay = append(replay, next)
			openToolUseIDs = currentToolUseIDs
		case "user":
			content := []message.Block{}
			resolvedToolUseIDs := map[string]struct{}{}
			hasUserInput := false
			for _, block := range msg.Content {
				switch block.Type {
				case "text":
					if strings.TrimSpace(block.Text) != "" {
						hasUserInput = true
						content = append(content, message.CloneBlock(block))
					}
				case "image", "document":
					hasUserInput = true
					supported := supportsImageInput
					label := "image input"
					defaultMIME := "image"
					if block.Type == "document" {
						supported = supportsPDFInput
						label = "PDF input"
						defaultMIME = "application/pdf"
					}
					if supported {
						content = append(content, message.CloneBlock(block))
						continue
					}
					attachment := fmt.Sprintf(
						"<file name=\"%s\" media_type=\"%s\" kind=\"%s\">Current model does not support %s.</file>",
						escapeAttachmentAttr(defaultString(block.Name, "attached-"+block.Type)),
						escapeAttachmentAttr(defaultString(block.MIMEType, defaultMIME)),
						block.Type,
						label,
					)
					content = append(content, message.TextBlock(attachment, map[string]any{"attachment": true}))
				case "tool_result":
					if block.ToolUseID == "" {
						continue
					}
					if _, ok := emittedToolUseIDs[block.ToolUseID]; !ok {
						continue
					}
					if _, ok := emittedToolResultIDs[block.ToolUseID]; ok {
						continue
					}
					next := message.CloneBlock(block)
					if !supportsImageInput && len(next.Content) > 0 {
						filtered := next.Content[:0]
						for _, child := range next.Content {
							if child.Type != "image" {
								filtered = append(filtered, child)
							}
						}
						next.Content = append([]message.Block(nil), filtered...)
					}
					content = append(content, next)
					resolvedToolUseIDs[block.ToolUseID] = struct{}{}
					emittedToolResultIDs[block.ToolUseID] = struct{}{}
				}
			}

			if hasUserInput && len(openToolUseIDs) > 0 {
				missing := []string{}
				for _, id := range openToolUseIDs {
					if _, ok := resolvedToolUseIDs[id]; !ok {
						missing = append(missing, id)
					}
				}
				if len(missing) > 0 {
					replay = append(replay, interruptedToolResultMessage(missing))
					for _, id := range missing {
						emittedToolResultIDs[id] = struct{}{}
					}
				}
				openToolUseIDs = nil
			} else if len(openToolUseIDs) > 0 {
				pending := []string{}
				for _, id := range openToolUseIDs {
					if _, ok := resolvedToolUseIDs[id]; !ok {
						pending = append(pending, id)
					}
				}
				openToolUseIDs = pending
			}

			if len(content) == 0 {
				if len(replay) > 0 && replay[len(replay)-1].Role == "assistant" {
					replay = append(replay, message.BuildMessage("user", []message.Block{
						message.TextBlock("[User turn omitted during replay]", nil),
					}, map[string]any{"synthetic": true}))
				}
				continue
			}

			next := message.Clone(msg)
			next.Content = content
			replay = append(replay, next)
		}
	}

	if len(openToolUseIDs) > 0 {
		replay = append(replay, interruptedToolResultMessage(openToolUseIDs))
	}

	return replay
}

// ToolResultContentBlocks returns structured tool result content or falls back to one text block.
func ToolResultContentBlocks(block message.Block) []message.Block {
	if len(block.Content) > 0 {
		out := make([]message.Block, len(block.Content))
		for i, child := range block.Content {
			out[i] = message.CloneBlock(child)
		}
		return out
	}
	return []message.Block{message.TextBlock(block.ModelText, nil)}
}

func interruptedToolResultMessage(toolUseIDs []string) message.Message {
	blocks := make([]message.Block, 0, len(toolUseIDs))
	for _, toolUseID := range toolUseIDs {
		blocks = append(blocks, message.ToolResultBlock(
			toolUseID,
			"error: tool call was interrupted",
			"Tool call was interrupted",
			true,
			nil,
			nil,
		))
	}
	return message.BuildMessage("user", blocks, nil)
}

func escapeAttachmentAttr(value string) string {
	return strings.NewReplacer(
		"&", "&amp;",
		`"`, "&quot;",
		"<", "&lt;",
		">", "&gt;",
	).Replace(value)
}

func defaultString(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}

func blockNativeMeta(block message.Block) map[string]any {
	raw, _ := block.Meta["native"].(map[string]any)
	if raw == nil {
		return map[string]any{}
	}
	return raw
}

func messageNativeMeta(msg message.Message) map[string]any {
	raw, _ := msg.Meta["native"].(map[string]any)
	if raw == nil {
		return map[string]any{}
	}
	return raw
}

func loadImageBlockPayload(block message.Block) (mimeType string, data string) {
	return defaultString(block.MIMEType, "image/png"), block.Data
}

func loadDocumentBlockPayload(block message.Block) (mimeType string, data string, name string) {
	return defaultString(block.MIMEType, "application/pdf"), block.Data, defaultString(block.Name, "document.pdf")
}

func decodeBase64(data string) []byte {
	decoded, err := base64.StdEncoding.DecodeString(data)
	if err != nil {
		return nil
	}
	return decoded
}

func dumpJSON(value any) any {
	if value == nil {
		return nil
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return nil
	}
	var out any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

func dumpJSONMap(value any) map[string]any {
	out, _ := dumpJSON(value).(map[string]any)
	return out
}

func unmarshalJSONMap(raw string) map[string]any {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil
	}
	return out
}
