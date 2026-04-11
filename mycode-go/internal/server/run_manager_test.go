package server

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	agentpkg "github.com/legibet/mycode-go/internal/agent"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/provider"
)

type blockingAdapter struct {
	spec    provider.Spec
	release chan struct{}
}

func (a *blockingAdapter) Spec() provider.Spec {
	return a.spec
}

func (a *blockingAdapter) StreamTurn(ctx context.Context, _ provider.Request) <-chan provider.StreamEvent {
	out := make(chan provider.StreamEvent, 2)
	go func() {
		defer close(out)
		out <- provider.StreamEvent{Type: "text_delta", Text: "reply"}
		select {
		case <-ctx.Done():
			return
		case <-a.release:
		}
		msg := message.AssistantMessage([]message.Block{message.TextBlock("reply", nil)}, "openai", "gpt-5.4", "", "", nil, nil)
		out <- provider.StreamEvent{Type: "message_done", Msg: &msg}
	}()
	return out
}

type completeAdapter struct {
	spec provider.Spec
}

func (a *completeAdapter) Spec() provider.Spec {
	return a.spec
}

func (a *completeAdapter) StreamTurn(_ context.Context, _ provider.Request) <-chan provider.StreamEvent {
	out := make(chan provider.StreamEvent, 2)
	go func() {
		defer close(out)
		out <- provider.StreamEvent{Type: "text_delta", Text: "reply"}
		msg := message.AssistantMessage([]message.Block{message.TextBlock("reply", nil)}, "openai", "gpt-5.4", "", "", nil, nil)
		out <- provider.StreamEvent{Type: "message_done", Msg: &msg}
	}()
	return out
}

func newTestAgent(t *testing.T, adapter provider.Adapter) *agentpkg.Agent {
	t.Helper()
	dir := t.TempDir()
	agent, err := agentpkg.New(agentpkg.Options{
		Model:              "gpt-5.4",
		Provider:           "openai",
		CWD:                dir,
		SessionDir:         filepath.Join(dir, "session"),
		SessionID:          "session",
		System:             "system",
		MaxTokens:          4096,
		ContextWindow:      128000,
		CompactThreshold:   0.8,
		SupportsImageInput: true,
		SupportsPDFInput:   true,
		Adapter:            adapter,
	})
	if err != nil {
		t.Fatal(err)
	}
	return agent
}

func waitFor(t *testing.T, deadline time.Duration, fn func() bool) {
	t.Helper()
	end := time.Now().Add(deadline)
	for time.Now().Before(end) {
		if fn() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met before timeout")
}

func TestRunManagerSnapshotIncludesUserMessageAndPendingEvents(t *testing.T) {
	manager := newRunManager()
	adapter := &blockingAdapter{
		spec:    provider.Spec{ID: "openai"},
		release: make(chan struct{}),
	}
	agent := newTestAgent(t, adapter)
	userMessage := message.UserTextMessage("build feature", nil)
	baseMessages := []message.Message{
		message.AssistantMessage([]message.Block{message.TextBlock("Earlier", nil)}, "openai", "gpt-5.4", "", "", nil, nil),
	}

	run, err := manager.startRun("session-1", userMessage, baseMessages, agent, func(message.Message) error { return nil })
	if err != nil {
		t.Fatal(err)
	}

	var snapshot *runSnapshot
	waitFor(t, time.Second, func() bool {
		snapshot = manager.snapshotSession("session-1")
		return snapshot != nil && len(snapshot.PendingEvents) > 0
	})

	if snapshot.Run["id"] != run["id"] {
		t.Fatalf("unexpected run info: %#v", snapshot.Run)
	}
	if len(snapshot.Messages) != 2 || snapshot.Messages[0].Content[0].Text != "Earlier" || snapshot.Messages[1].Content[0].Text != "build feature" {
		t.Fatalf("unexpected snapshot messages: %#v", snapshot.Messages)
	}
	if len(snapshot.PendingEvents) != 1 || snapshot.PendingEvents[0]["type"] != "text" || snapshot.PendingEvents[0]["delta"] != "reply" {
		t.Fatalf("unexpected pending events: %#v", snapshot.PendingEvents)
	}

	close(adapter.release)
	waitFor(t, time.Second, func() bool {
		state := manager.getRun(run["id"].(string))
		return state != nil && state.info()["status"] == "completed"
	})
}

func TestRunManagerSameSessionCannotStartSecondRun(t *testing.T) {
	manager := newRunManager()
	first := newTestAgent(t, &blockingAdapter{
		spec:    provider.Spec{ID: "openai"},
		release: make(chan struct{}),
	})
	userMessage := message.UserTextMessage("first", nil)

	run, err := manager.startRun("session-1", userMessage, nil, first, func(message.Message) error { return nil })
	if err != nil {
		t.Fatal(err)
	}

	second := newTestAgent(t, &completeAdapter{spec: provider.Spec{ID: "openai"}})
	if _, err := manager.startRun("session-1", message.UserTextMessage("second", nil), nil, second, func(message.Message) error { return nil }); err == nil {
		t.Fatal("expected activeRunError")
	}

	state := manager.getRun(run["id"].(string))
	close(first.Adapter.(*blockingAdapter).release)
	waitFor(t, time.Second, func() bool {
		return state != nil && state.info()["status"] == "completed"
	})
}

func TestRunManagerCancelOnlyMarksTargetRunCancelled(t *testing.T) {
	manager := newRunManager()
	firstAdapter := &blockingAdapter{spec: provider.Spec{ID: "openai"}, release: make(chan struct{})}
	secondAdapter := &blockingAdapter{spec: provider.Spec{ID: "openai"}, release: make(chan struct{})}
	first := newTestAgent(t, firstAdapter)
	second := newTestAgent(t, secondAdapter)

	firstRun, err := manager.startRun("session-1", message.UserTextMessage("first", nil), nil, first, func(message.Message) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	secondRun, err := manager.startRun("session-2", message.UserTextMessage("second", nil), nil, second, func(message.Message) error { return nil })
	if err != nil {
		t.Fatal(err)
	}

	cancelled := manager.cancelRun(firstRun["id"].(string))
	if cancelled == nil {
		t.Fatal("expected cancelled run info")
	}

	waitFor(t, time.Second, func() bool {
		state := manager.getRun(firstRun["id"].(string))
		return state != nil && state.info()["status"] == "cancelled"
	})

	updatedFirst := manager.getRun(firstRun["id"].(string))
	updatedSecond := manager.getRun(secondRun["id"].(string))
	if updatedFirst.info()["status"] != "cancelled" {
		t.Fatalf("unexpected first run: %#v", updatedFirst.info())
	}
	if updatedSecond.info()["status"] != "running" {
		t.Fatalf("unexpected second run: %#v", updatedSecond.info())
	}

	close(secondAdapter.release)
	waitFor(t, time.Second, func() bool {
		state := manager.getRun(secondRun["id"].(string))
		return state != nil && state.info()["status"] == "completed"
	})
}

func TestRunManagerFinishedRunStaysAvailableForReconnectWindow(t *testing.T) {
	manager := newRunManager()
	agent := newTestAgent(t, &completeAdapter{spec: provider.Spec{ID: "openai"}})

	run, err := manager.startRun("session-1", message.UserTextMessage("done", nil), nil, agent, func(message.Message) error { return nil })
	if err != nil {
		t.Fatal(err)
	}

	waitFor(t, time.Second, func() bool {
		state := manager.getRun(run["id"].(string))
		return state != nil && state.info()["status"] == "completed"
	})

	finished := manager.getRun(run["id"].(string))
	if finished == nil || finished.info()["status"] != "completed" {
		t.Fatalf("unexpected finished run: %#v", finished)
	}
	if snapshot := manager.snapshotSession("session-1"); snapshot != nil {
		t.Fatalf("expected no active snapshot: %#v", snapshot)
	}
}
