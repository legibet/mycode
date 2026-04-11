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

// Options configures one agent instance.
type Options struct {
	Model              string
	Provider           string
	CWD                string
	SessionDir         string
	SessionID          string
	APIKey             string
	APIBase            string
	System             string
	Messages           []message.Message
	MaxTurns           int
	MaxTokens          int
	ContextWindow      int
	CompactThreshold   float64
	ReasoningEffort    string
	SupportsImageInput bool
	SupportsPDFInput   bool
	Adapter            provider.Adapter
	Tools              *tools.Executor
}

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

// New creates an agent from options.
func New(opts Options) (*Agent, error) {
	sessionDir := opts.SessionDir
	if sessionDir == "" {
		sessionDir = opts.CWD
	}
	sessionID := opts.SessionID
	if sessionID == "" {
		sessionID = filepath.Base(sessionDir)
	}

	toolExecutor := opts.Tools
	if toolExecutor == nil {
		toolExecutor = tools.NewExecutor(opts.CWD, sessionDir, opts.SupportsImageInput)
	}

	adapter := opts.Adapter
	if adapter == nil {
		var ok bool
		adapter, ok = provider.LookupAdapter(opts.Provider)
		if !ok {
			return nil, errors.New("unsupported provider adapter: " + opts.Provider)
		}
	}

	system := opts.System
	if system == "" {
		system = prompt.Build(opts.CWD, config.ResolveHome())
	}

	cloned := make([]message.Message, len(opts.Messages))
	for i, msg := range opts.Messages {
		cloned[i] = message.Clone(msg)
	}

	return &Agent{
		Model:              opts.Model,
		Provider:           opts.Provider,
		CWD:                opts.CWD,
		SessionDir:         sessionDir,
		SessionID:          sessionID,
		APIKey:             opts.APIKey,
		APIBase:            opts.APIBase,
		MaxTurns:           opts.MaxTurns,
		MaxTokens:          opts.MaxTokens,
		ContextWindow:      opts.ContextWindow,
		CompactThreshold:   opts.CompactThreshold,
		ReasoningEffort:    opts.ReasoningEffort,
		SupportsImageInput: opts.SupportsImageInput,
		SupportsPDFInput:   opts.SupportsPDFInput,
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

		// persist saves a message and emits an error event on failure, returning false to abort.
		persist := func(msg message.Message) bool {
			if onPersist == nil {
				return true
			}
			if err := onPersist(msg); err != nil {
				out <- Event{Type: "error", Data: map[string]any{"message": err.Error()}}
				return false
			}
			return true
		}

		a.Messages = append(a.Messages, message.Clone(userInput))
		if !persist(userInput) {
			return
		}

		completed := false
		for turn := 0; a.MaxTurns <= 0 || turn < a.MaxTurns; turn++ {
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
					msg := "provider error"
					if event.Err != nil {
						msg = event.Err.Error()
					}
					out <- Event{Type: "error", Data: map[string]any{"message": msg}}
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
			if !persist(*assistant) {
				return
			}

			toolCalls := make([]message.Block, 0, len(assistant.Content))
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
			if !persist(toolMessage) {
				return
			}
		}

		if !completed && a.MaxTurns > 0 {
			out <- Event{Type: "error", Data: map[string]any{"message": "max_turns reached"}}
			return
		}

		a.compactIfNeeded(ctx, onPersist, out)
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

func (a *Agent) compactIfNeeded(ctx context.Context, onPersist PersistFunc, out chan<- Event) {
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
}

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
