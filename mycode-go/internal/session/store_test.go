package session

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/legibet/mycode-go/internal/message"
)

func TestNewStoreUsesMycodeHome(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, ".mycode")
	t.Setenv("MYCODE_HOME", home)

	store := NewStore("")
	expected := filepath.Join(home, "sessions")
	if store.dataDir != expected {
		t.Fatalf("unexpected data dir: %s", store.dataDir)
	}
	if _, err := os.Stat(expected); err != nil {
		t.Fatalf("expected sessions dir: %v", err)
	}
}

func TestShouldCompactAcceptsProviderSpecificUsageShapes(t *testing.T) {
	cases := []map[string]any{
		{"input_tokens": 5000},
		{"prompt_tokens": 7000},
		{"prompt_token_count": 3000},
	}
	for _, usage := range cases {
		if !ShouldCompact(usage, 10000, 0.3) {
			t.Fatalf("expected compact for usage %#v", usage)
		}
	}
}

func TestLoadSessionRepairsInterruptedToolLoop(t *testing.T) {
	store := NewStore(t.TempDir())
	data, err := store.CreateSession("Test", "", "anthropic", "gpt-5.4", "/tmp", "")
	if err != nil {
		t.Fatal(err)
	}
	sessionID := data.Session.ID

	if err := store.AppendMessage(sessionID, message.AssistantMessage([]message.Block{
		message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
	}, "anthropic", "gpt-5.4", "", "", nil, nil), "anthropic", "gpt-5.4", "/tmp", ""); err != nil {
		t.Fatal(err)
	}

	loaded, err := store.LoadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if loaded == nil || len(loaded.Messages) != 2 {
		t.Fatalf("unexpected session: %#v", loaded)
	}
	if loaded.Messages[1].Role != "user" || loaded.Messages[1].Content[0].Type != "tool_result" {
		t.Fatalf("unexpected repaired message: %#v", loaded.Messages)
	}

	loadedAgain, err := store.LoadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if len(loadedAgain.Messages) != 2 {
		t.Fatalf("unexpected repaired session on reload: %#v", loadedAgain.Messages)
	}
}

func TestAppendRewindNonexistentSessionIsNoop(t *testing.T) {
	store := NewStore(t.TempDir())
	if err := store.AppendRewind("missing", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(store.SessionDir("missing")); !os.IsNotExist(err) {
		t.Fatalf("expected no session dir, got err=%v", err)
	}
}

func TestApplyRewindMultipleMarkers(t *testing.T) {
	messages := []message.Message{
		message.UserTextMessage("a", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("b", nil)}, "p", "m", "", "", nil, nil),
		message.UserTextMessage("c", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("d", nil)}, "p", "m", "", "", nil, nil),
		BuildRewindEvent(2),
		message.UserTextMessage("e", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("f", nil)}, "p", "m", "", "", nil, nil),
		BuildRewindEvent(2),
		message.UserTextMessage("g", nil),
	}

	visible := ApplyRewind(messages)
	if len(visible) != 3 || visible[0].Content[0].Text != "a" || visible[1].Content[0].Text != "b" || visible[2].Content[0].Text != "g" {
		t.Fatalf("unexpected rewind result: %#v", visible)
	}
}

func TestLoadSessionAppliesLatestCompactSummary(t *testing.T) {
	store := NewStore(t.TempDir())
	data, err := store.CreateSession("Test", "", "p", "m", "/tmp", "")
	if err != nil {
		t.Fatal(err)
	}
	sessionID := data.Session.ID

	for _, msg := range []message.Message{
		message.UserTextMessage("hello", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("hi", nil)}, "p", "m", "", "", nil, nil),
		BuildCompactEvent("old summary", "p", "m", 2, nil),
		message.UserTextMessage("next", nil),
		BuildCompactEvent("new summary", "p", "m", 4, nil),
		message.AssistantMessage([]message.Block{message.TextBlock("latest reply", nil)}, "p", "m", "", "", nil, nil),
	} {
		if err := store.AppendMessage(sessionID, msg, "p", "m", "/tmp", ""); err != nil {
			t.Fatal(err)
		}
	}

	loaded, err := store.LoadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if len(loaded.Messages) != 3 {
		t.Fatalf("unexpected compacted messages: %#v", loaded.Messages)
	}
	if loaded.Messages[0].Role != "user" || loaded.Messages[0].Meta["synthetic"] != true || loaded.Messages[0].Content[0].Text != "[Conversation Summary]\n\nnew summary" {
		t.Fatalf("unexpected summary message: %#v", loaded.Messages[0])
	}
	if loaded.Messages[1].Role != "assistant" || loaded.Messages[1].Content[0].Text != compactAck {
		t.Fatalf("unexpected ack: %#v", loaded.Messages[1])
	}
}

func TestRewindAfterCompactPreservesSummary(t *testing.T) {
	store := NewStore(t.TempDir())
	data, err := store.CreateSession("Test", "", "p", "m", "/tmp", "")
	if err != nil {
		t.Fatal(err)
	}
	sessionID := data.Session.ID

	for _, msg := range []message.Message{
		message.UserTextMessage("hello", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("hi", nil)}, "p", "m", "", "", nil, nil),
		BuildCompactEvent("summary of hello+hi", "p", "m", 2, nil),
		message.UserTextMessage("explain X", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("X is...", nil)}, "p", "m", "", "", nil, nil),
	} {
		if err := store.AppendMessage(sessionID, msg, "p", "m", "/tmp", ""); err != nil {
			t.Fatal(err)
		}
	}

	loaded, err := store.LoadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if len(loaded.Messages) != 4 {
		t.Fatalf("unexpected loaded messages: %#v", loaded.Messages)
	}

	if err := store.AppendRewind(sessionID, 2); err != nil {
		t.Fatal(err)
	}
	if err := store.AppendMessage(sessionID, message.UserTextMessage("explain Y instead", nil), "p", "m", "/tmp", ""); err != nil {
		t.Fatal(err)
	}

	reloaded, err := store.LoadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if len(reloaded.Messages) != 3 {
		t.Fatalf("unexpected reloaded messages: %#v", reloaded.Messages)
	}
	if reloaded.Messages[0].Meta["synthetic"] != true || reloaded.Messages[1].Role != "assistant" || reloaded.Messages[2].Content[0].Text != "explain Y instead" {
		t.Fatalf("unexpected rewind after compact result: %#v", reloaded.Messages)
	}
}

func TestRewindPastInterruptedToolLoopSkipsRepair(t *testing.T) {
	store := NewStore(t.TempDir())
	data, err := store.CreateSession("Test", "", "p", "m", "/tmp", "")
	if err != nil {
		t.Fatal(err)
	}
	sessionID := data.Session.ID

	for _, msg := range []message.Message{
		message.UserTextMessage("hello", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("hi", nil)}, "p", "m", "", "", nil, nil),
		message.AssistantMessage([]message.Block{
			message.ToolUseBlock("call_1", "read", map[string]any{"path": "x.py"}, nil),
		}, "p", "m", "", "", nil, nil),
	} {
		if err := store.AppendMessage(sessionID, msg, "p", "m", "/tmp", ""); err != nil {
			t.Fatal(err)
		}
	}

	if err := store.AppendRewind(sessionID, 2); err != nil {
		t.Fatal(err)
	}

	loaded, err := store.LoadSession(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if len(loaded.Messages) != 2 || loaded.Messages[0].Content[0].Text != "hello" || loaded.Messages[1].Content[0].Text != "hi" {
		t.Fatalf("unexpected messages after rewind: %#v", loaded.Messages)
	}

	lines, err := os.ReadFile(filepath.Join(store.SessionDir(sessionID), "messages.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, line := range bytesToLines(lines) {
		var msg map[string]any
		if err := json.Unmarshal([]byte(line), &msg); err == nil && msg["role"] == "user" {
			if content, ok := msg["content"].([]any); ok && len(content) > 0 {
				block, _ := content[0].(map[string]any)
				if block["type"] == "tool_result" {
					count++
				}
			}
		}
	}
	if count != 0 {
		t.Fatalf("unexpected repaired tool results persisted: %d", count)
	}
}

func bytesToLines(data []byte) []string {
	lines := []string{}
	start := 0
	for i, b := range data {
		if b != '\n' {
			continue
		}
		if i > start {
			lines = append(lines, string(data[start:i]))
		}
		start = i + 1
	}
	if start < len(data) {
		lines = append(lines, string(data[start:]))
	}
	return lines
}
