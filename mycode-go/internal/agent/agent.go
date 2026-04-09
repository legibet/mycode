package agent

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"path/filepath"

	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/prompt"
	"github.com/legibet/mycode-go/internal/provider"
	"github.com/legibet/mycode-go/internal/session"
	"github.com/legibet/mycode-go/internal/tools"
)

// Event is the normalized streaming event sent to the API and CLI.
type Event struct {
	Type string
	Data map[string]any
}

// PersistFunc stores one canonical message.
type PersistFunc func(message.Message) error

// Agent is the single orchestration loop.
type Agent struct {
	Model              string
	Provider           string
	CWD                string
	SessionDir         string
	SessionID          string
	APIKey             string
	APIBase            string
	MaxTurns           int
	MaxTokens          int
	ContextWindow      int
	CompactThreshold   float64
	ReasoningEffort    string
	SupportsImageInput bool
	SupportsPDFInput   bool
	System             string
	Messages           []message.Message
	Tools              *tools.Executor
	Adapter            provider.Adapter
}

// New creates an agent.
func New(
	model, providerType, cwd, sessionDir, sessionID, apiKey, apiBase, system string,
	messages []message.Message,
	maxTurns, maxTokens, contextWindow int,
	compactThreshold float64,
	reasoningEffort string,
	supportsImageInput, supportsPDFInput bool,
	adapter provider.Adapter,
	toolExecutor *tools.Executor,
) (*Agent, error) {
	resolvedSessionDir := sessionDir
	if resolvedSessionDir == "" {
		resolvedSessionDir = cwd
	}
	resolvedSessionID := sessionID
	if resolvedSessionID == "" {
		resolvedSessionID = filepath.Base(resolvedSessionDir)
	}
	if toolExecutor == nil {
		toolExecutor = tools.NewExecutor(cwd, resolvedSessionDir, supportsImageInput)
	}
	if adapter == nil {
		var ok bool
		adapter, ok = provider.LookupAdapter(providerType)
		if !ok {
			return nil, errors.New("unsupported provider adapter: " + providerType)
		}
	}
	if system == "" {
		system = prompt.Build(cwd, config.ResolveHome())
	}
	cloned := make([]message.Message, len(messages))
	for i, msg := range messages {
		cloned[i] = message.Clone(msg)
	}
	return &Agent{
		Model:              model,
		Provider:           providerType,
		CWD:                cwd,
		SessionDir:         resolvedSessionDir,
		SessionID:          resolvedSessionID,
		APIKey:             apiKey,
		APIBase:            apiBase,
		MaxTurns:           maxTurns,
		MaxTokens:          maxTokens,
		ContextWindow:      contextWindow,
		CompactThreshold:   compactThreshold,
		ReasoningEffort:    reasoningEffort,
		SupportsImageInput: supportsImageInput,
		SupportsPDFInput:   supportsPDFInput,
		System:             system,
		Messages:           cloned,
		Tools:              toolExecutor,
		Adapter:            adapter,
	}, nil
}

// Cancel stops active tools. Provider cancellation is driven by ctx.
func (a *Agent) Cancel() {
	a.Tools.CancelActive()
}

// Chat runs one user turn.
func (a *Agent) Chat(ctx context.Context, userInput message.Message, onPersist PersistFunc) <-chan Event {
	out := make(chan Event, 32)
	go func() {
		defer close(out)
		if userInput.Role != "user" {
			out <- Event{Type: "error", Data: map[string]any{"message": "user input must be a user message"}}
			return
		}
		if err := a.validateUserInput(userInput); err != nil {
			out <- Event{Type: "error", Data: map[string]any{"message": err.Error()}}
			return
		}

		a.Messages = append(a.Messages, message.Clone(userInput))
		if onPersist != nil {
			if err := onPersist(userInput); err != nil {
				out <- Event{Type: "error", Data: map[string]any{"message": err.Error()}}
				return
			}
		}

		turn := 0
		completed := false
		for a.MaxTurns <= 0 || turn < a.MaxTurns {
			turn++
			req := provider.Request{
				Provider:           a.Provider,
				Model:              a.Model,
				SessionID:          a.SessionID,
				Messages:           a.Messages,
				System:             a.System,
				Tools:              toolSpecs(a.Tools.Definitions()),
				MaxTokens:          a.MaxTokens,
				APIKey:             a.APIKey,
				APIBase:            a.APIBase,
				ReasoningEffort:    a.ReasoningEffort,
				SupportsImageInput: a.SupportsImageInput,
				SupportsPDFInput:   a.SupportsPDFInput,
			}

			var assistant *message.Message
			for event := range a.Adapter.StreamTurn(ctx, req) {
				switch event.Type {
				case "thinking_delta":
					if event.Text != "" {
						out <- Event{Type: "reasoning", Data: map[string]any{"delta": event.Text}}
					}
				case "text_delta":
					if event.Text != "" {
						out <- Event{Type: "text", Data: map[string]any{"delta": event.Text}}
					}
				case "message_done":
					assistant = event.Msg
				case "provider_error":
					if event.Err != nil {
						out <- Event{Type: "error", Data: map[string]any{"message": event.Err.Error()}}
					} else {
						out <- Event{Type: "error", Data: map[string]any{"message": "provider error"}}
					}
					return
				}
			}
			if ctx.Err() != nil {
				out <- Event{Type: "error", Data: map[string]any{"message": "cancelled"}}
				return
			}
			if assistant == nil {
				out <- Event{Type: "error", Data: map[string]any{"message": "provider produced no assistant message"}}
				return
			}

			a.Messages = append(a.Messages, message.Clone(*assistant))
			if onPersist != nil {
				if err := onPersist(*assistant); err != nil {
					out <- Event{Type: "error", Data: map[string]any{"message": err.Error()}}
					return
				}
			}

			toolCalls := make([]message.Block, 0)
			for _, block := range assistant.Content {
				if block.Type == "tool_use" {
					toolCalls = append(toolCalls, block)
				}
			}
			if len(toolCalls) == 0 {
				completed = true
				break
			}

			toolResults := make([]message.Block, 0, len(toolCalls))
			for _, toolCall := range toolCalls {
				select {
				case <-ctx.Done():
					out <- Event{Type: "error", Data: map[string]any{"message": "cancelled"}}
					return
				default:
				}

				out <- Event{Type: "tool_start", Data: map[string]any{
					"tool_call": map[string]any{
						"id":    toolCall.ID,
						"name":  toolCall.Name,
						"input": cloneInput(toolCall.Input),
					},
				}}

				result := a.runTool(toolCall, out)
				toolResults = append(toolResults, message.ToolResultBlock(
					toolCall.ID,
					result.ModelText,
					result.DisplayText,
					result.IsError,
					result.Content,
					nil,
				))

				data := map[string]any{
					"tool_use_id":  toolCall.ID,
					"model_text":   result.ModelText,
					"display_text": result.DisplayText,
					"is_error":     result.IsError,
				}
				if len(result.Content) > 0 {
					data["content"] = result.Content
				}
				out <- Event{Type: "tool_done", Data: data}

				if result.ModelText == "error: cancelled" && ctx.Err() != nil {
					toolMessage := message.BuildMessage("user", toolResults, nil)
					a.Messages = append(a.Messages, toolMessage)
					if onPersist != nil {
						_ = onPersist(toolMessage)
					}
					return
				}
			}

			toolMessage := message.BuildMessage("user", toolResults, nil)
			a.Messages = append(a.Messages, toolMessage)
			if onPersist != nil {
				if err := onPersist(toolMessage); err != nil {
					out <- Event{Type: "error", Data: map[string]any{"message": err.Error()}}
					return
				}
			}
		}
		if !completed && a.MaxTurns > 0 {
			out <- Event{Type: "error", Data: map[string]any{"message": "max_turns reached"}}
			return
		}

		for event := range a.compactIfNeeded(ctx, onPersist) {
			out <- event
		}
	}()
	return out
}

func (a *Agent) validateUserInput(userInput message.Message) error {
	for _, block := range userInput.Content {
		if block.Type == "image" && !a.SupportsImageInput {
			return fmt.Errorf("current model does not support image input")
		}
		if block.Type == "document" && !a.SupportsPDFInput {
			return fmt.Errorf("current model does not support PDF input")
		}
	}
	return nil
}

func (a *Agent) runTool(toolCall message.Block, out chan<- Event) tools.Result {
	switch toolCall.Name {
	case "read":
		return a.Tools.Read(asString(toolCall.Input["path"]), asInt(toolCall.Input["offset"]), asInt(toolCall.Input["limit"]))
	case "write":
		return a.Tools.Write(asString(toolCall.Input["path"]), asString(toolCall.Input["content"]))
	case "edit":
		return a.Tools.Edit(asString(toolCall.Input["path"]), asEdits(toolCall.Input["edits"]))
	case "bash":
		return a.Tools.Bash(toolCall.ID, asString(toolCall.Input["command"]), asInt(toolCall.Input["timeout"]), func(text string) {
			out <- Event{Type: "tool_output", Data: map[string]any{
				"tool_use_id": toolCall.ID,
				"output":      text,
			}}
		})
	default:
		return tools.Result{ModelText: "error: unknown tool: " + toolCall.Name, DisplayText: "Unknown tool: " + toolCall.Name, IsError: true}
	}
}

func (a *Agent) compactIfNeeded(ctx context.Context, onPersist PersistFunc) <-chan Event {
	out := make(chan Event, 1)
	go func() {
		defer close(out)
		if len(a.Messages) == 0 {
			return
		}

		var usage map[string]any
		for i := len(a.Messages) - 1; i >= 0; i-- {
			msg := a.Messages[i]
			if msg.Role != "assistant" {
				continue
			}
			if raw, ok := msg.Meta["usage"].(map[string]any); ok {
				usage = raw
			}
			break
		}
		if !session.ShouldCompact(usage, a.ContextWindow, a.CompactThreshold) {
			return
		}

		beforeCount := len(a.Messages)
		compactMessages := append(append([]message.Message(nil), a.Messages...), message.UserTextMessage(session.CompactSummaryPrompt, nil))
		req := provider.Request{
			Provider:           a.Provider,
			Model:              a.Model,
			SessionID:          a.SessionID,
			Messages:           compactMessages,
			System:             a.System,
			MaxTokens:          min(a.MaxTokens, 8192),
			APIKey:             a.APIKey,
			APIBase:            a.APIBase,
			SupportsImageInput: a.SupportsImageInput,
			SupportsPDFInput:   a.SupportsPDFInput,
		}

		var summary *message.Message
		for event := range a.Adapter.StreamTurn(ctx, req) {
			if event.Type == "message_done" {
				summary = event.Msg
			}
			if event.Type == "provider_error" {
				return
			}
		}
		if ctx.Err() != nil || summary == nil {
			return
		}
		summaryText := message.FlattenText(*summary, false)
		if summaryText == "" {
			return
		}
		compactEvent := session.BuildCompactEvent(summaryText, a.Provider, a.Model, beforeCount, summary.Meta["usage"])
		if onPersist != nil {
			if err := onPersist(compactEvent); err != nil {
				return
			}
		}
		a.Messages = append(a.Messages, compactEvent)
		a.Messages = session.ApplyCompact(a.Messages)
		out <- Event{Type: "compact", Data: map[string]any{
			"message":         fmt.Sprintf("Context compacted (%d messages -> summary)", beforeCount),
			"compacted_count": beforeCount,
		}}
	}()
	return out
}

// toolSpecs converts internal tool definitions to provider-facing maps.
func toolSpecs(specs []tools.ToolSpec) []map[string]any {
	out := make([]map[string]any, 0, len(specs))
	for _, spec := range specs {
		out = append(out, map[string]any{
			"name":         spec.Name,
			"description":  spec.Description,
			"input_schema": spec.InputSchema,
		})
	}
	return out
}

func cloneInput(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	return maps.Clone(input)
}

func asString(value any) string {
	text, _ := value.(string)
	return text
}

func asInt(value any) int {
	switch v := value.(type) {
	case int:
		return v
	case float64:
		return int(v)
	default:
		return 0
	}
}

func asEdits(value any) []map[string]string {
	items, _ := value.([]any)
	out := make([]map[string]string, 0, len(items))
	for _, item := range items {
		entry, _ := item.(map[string]any)
		if entry == nil {
			continue
		}
		out = append(out, map[string]string{
			"oldText": asString(entry["oldText"]),
			"newText": asString(entry["newText"]),
		})
	}
	return out
}
