package agent

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/provider"
)

type fakeAdapter struct {
	spec     provider.Spec
	turns    [][]provider.StreamEvent
	requests []provider.Request
}

func (f *fakeAdapter) Spec() provider.Spec {
	return f.spec
}

func (f *fakeAdapter) StreamTurn(_ context.Context, req provider.Request) <-chan provider.StreamEvent {
	out := make(chan provider.StreamEvent, 8)
	f.requests = append(f.requests, req)
	events := []provider.StreamEvent{}
	if len(f.turns) > 0 {
		events = f.turns[0]
		f.turns = f.turns[1:]
	}
	go func() {
		defer close(out)
		for _, event := range events {
			out <- event
		}
	}()
	return out
}

func TestChatPersistsReasoningBlocks(t *testing.T) {
	dir := t.TempDir()
	agent, err := New(
		"gpt-5.4",
		"openai",
		dir,
		filepath.Join(dir, "session"),
		"session",
		"",
		"",
		"",
		nil,
		0,
		4096,
		128000,
		0.8,
		"",
		true,
		true,
		&fakeAdapter{
			spec: provider.Spec{ID: "openai"},
			turns: [][]provider.StreamEvent{{
				{Type: "thinking_delta", Text: "hidden "},
				{Type: "text_delta", Text: "Visible answer"},
				{Type: "message_done", Msg: ptrMessage(message.AssistantMessage([]message.Block{
					message.ThinkingBlock("hidden ", nil),
					message.TextBlock("Visible answer", nil),
				}, "openai", "gpt-5.4", "", "", nil, nil))},
			}},
		},
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	persisted := []message.Message{}
	events := collectEvents(agent.Chat(context.Background(), message.UserTextMessage("hello", nil), func(msg message.Message) error {
		persisted = append(persisted, msg)
		return nil
	}))

	if len(events) != 2 || events[0].Type != "reasoning" || events[1].Type != "text" {
		t.Fatalf("unexpected events: %#v", events)
	}
	if len(persisted) < 2 || len(persisted[1].Content) != 2 || persisted[1].Content[0].Type != "thinking" {
		t.Fatalf("unexpected persisted messages: %#v", persisted)
	}
}

func TestChatRespectsExplicitTurnLimit(t *testing.T) {
	dir := t.TempDir()
	agent, err := New(
		"gpt-5.4",
		"openai",
		dir,
		filepath.Join(dir, "session"),
		"session",
		"",
		"",
		"",
		nil,
		2,
		4096,
		128000,
		0.8,
		"",
		true,
		true,
		&fakeAdapter{
			spec: provider.Spec{ID: "openai"},
			turns: [][]provider.StreamEvent{
				{{
					Type: "message_done",
					Msg: ptrMessage(message.AssistantMessage([]message.Block{
						message.ToolUseBlock("call-1", "read", map[string]any{"path": "missing.txt"}, nil),
					}, "openai", "gpt-5.4", "", "", nil, nil)),
				}},
				{{
					Type: "message_done",
					Msg: ptrMessage(message.AssistantMessage([]message.Block{
						message.ToolUseBlock("call-2", "read", map[string]any{"path": "missing.txt"}, nil),
					}, "openai", "gpt-5.4", "", "", nil, nil)),
				}},
			},
		},
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	events := collectEvents(agent.Chat(context.Background(), message.UserTextMessage("hello", nil), nil))
	last := events[len(events)-1]
	if last.Type != "error" || last.Data["message"] != "max_turns reached" {
		t.Fatalf("unexpected events: %#v", events)
	}
}

func TestChatPassesSessionIDToProviderRequest(t *testing.T) {
	dir := t.TempDir()
	adapter := &fakeAdapter{
		spec: provider.Spec{ID: "openai"},
		turns: [][]provider.StreamEvent{{
			{
				Type: "message_done",
				Msg:  ptrMessage(message.AssistantMessage([]message.Block{message.TextBlock("ok", nil)}, "openai", "gpt-5.4", "", "", nil, nil)),
			},
		}},
	}
	agent, err := New(
		"gpt-5.4",
		"openai",
		dir,
		filepath.Join(dir, "session-explicit"),
		"session-explicit",
		"",
		"",
		"",
		nil,
		0,
		4096,
		128000,
		0.8,
		"",
		true,
		true,
		adapter,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	collectEvents(agent.Chat(context.Background(), message.UserTextMessage("hello", nil), nil))

	if len(adapter.requests) != 1 || adapter.requests[0].SessionID != "session-explicit" {
		t.Fatalf("unexpected requests: %#v", adapter.requests)
	}
}

func TestCompactRequestOmitsReasoningEffort(t *testing.T) {
	dir := t.TempDir()
	adapter := &fakeAdapter{
		spec: provider.Spec{ID: "openai"},
		turns: [][]provider.StreamEvent{
			{{
				Type: "message_done",
				Msg: ptrMessage(message.AssistantMessage([]message.Block{
					message.TextBlock("answer", nil),
				}, "openai", "gpt-5.4", "", "", map[string]any{"input_tokens": 90}, nil)),
			}},
			{{
				Type: "message_done",
				Msg: ptrMessage(message.AssistantMessage([]message.Block{
					message.TextBlock("summary", nil),
				}, "openai", "gpt-5.4", "", "", nil, nil)),
			}},
		},
	}
	agent, err := New(
		"gpt-5.4",
		"openai",
		dir,
		filepath.Join(dir, "session"),
		"session",
		"",
		"",
		"",
		nil,
		0,
		4096,
		100,
		0.8,
		"high",
		true,
		true,
		adapter,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	events := collectEvents(agent.Chat(context.Background(), message.UserTextMessage("hello", nil), nil))

	if len(adapter.requests) != 2 {
		t.Fatalf("unexpected requests: %#v", adapter.requests)
	}
	if adapter.requests[0].ReasoningEffort != "high" {
		t.Fatalf("unexpected main request: %#v", adapter.requests[0])
	}
	if adapter.requests[1].ReasoningEffort != "" || len(adapter.requests[1].Tools) != 0 {
		t.Fatalf("unexpected compact request: %#v", adapter.requests[1])
	}
	last := events[len(events)-1]
	if last.Type != "compact" {
		t.Fatalf("unexpected events: %#v", events)
	}
}

func collectEvents(stream <-chan Event) []Event {
	events := []Event{}
	for event := range stream {
		events = append(events, event)
	}
	return events
}

func ptrMessage(msg message.Message) *message.Message {
	return &msg
}

func TestNewRejectsUnsupportedProviderAdapter(t *testing.T) {
	dir := t.TempDir()
	agent, err := New(
		"gpt-5.4",
		"missing",
		dir,
		filepath.Join(dir, "session"),
		"session",
		"",
		"",
		"",
		nil,
		0,
		4096,
		128000,
		0.8,
		"",
		true,
		true,
		nil,
		nil,
	)
	if err == nil {
		t.Fatalf("expected error, got agent %#v", agent)
	}
	if err.Error() != "unsupported provider adapter: missing" {
		t.Fatalf("unexpected error: %v", err)
	}
}
