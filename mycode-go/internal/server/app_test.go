package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/session"
)

func TestWorkspaceRoutes(t *testing.T) {
	root := t.TempDir()
	projectDir := filepath.Join(root, "projects", "demo")
	if err := os.MkdirAll(projectDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, ".hidden"), 0o755); err != nil {
		t.Fatal(err)
	}

	t.Setenv("MYCODE_WORKSPACE_ROOTS", root)
	handler := newApp(false, "", session.NewStore(t.TempDir()), newRunManager())

	t.Run("roots", func(t *testing.T) {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, "/api/workspaces/roots", nil)
		handler.ServeHTTP(recorder, request)

		if recorder.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d", recorder.Code)
		}

		var payload struct {
			Roots []string `json:"roots"`
		}
		decodeBody(t, recorder, &payload)
		if len(payload.Roots) != 1 || payload.Roots[0] != root {
			t.Fatalf("unexpected roots: %#v", payload.Roots)
		}
	})

	t.Run("browse", func(t *testing.T) {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(
			http.MethodGet,
			"/api/workspaces/browse?root="+root+"&path=projects",
			nil,
		)
		handler.ServeHTTP(recorder, request)

		if recorder.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d", recorder.Code)
		}

		var payload struct {
			Path    string `json:"path"`
			Current string `json:"current"`
			Entries []struct {
				Name string `json:"name"`
				Path string `json:"path"`
			} `json:"entries"`
			Error string `json:"error"`
		}
		decodeBody(t, recorder, &payload)
		if payload.Error != "" {
			t.Fatalf("unexpected error: %s", payload.Error)
		}
		if payload.Path != "projects" {
			t.Fatalf("unexpected path: %s", payload.Path)
		}
		if !strings.HasSuffix(payload.Current, filepath.Join("projects")) {
			t.Fatalf("unexpected current path: %s", payload.Current)
		}
		if len(payload.Entries) != 1 || payload.Entries[0].Name != "demo" || payload.Entries[0].Path != "projects/demo" {
			t.Fatalf("unexpected entries: %#v", payload.Entries)
		}
	})
}

func TestServeStaticFromConfiguredWebRoot(t *testing.T) {
	webRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(webRoot, "index.html"), []byte("index"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(webRoot, "app.js"), []byte("console.log('ok')"), 0o644); err != nil {
		t.Fatal(err)
	}

	handler := newApp(true, webRoot, session.NewStore(t.TempDir()), newRunManager())

	t.Run("asset", func(t *testing.T) {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, "/app.js", nil)
		handler.ServeHTTP(recorder, request)

		if recorder.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d", recorder.Code)
		}
		if body := recorder.Body.String(); body != "console.log('ok')" {
			t.Fatalf("unexpected asset body: %q", body)
		}
	})

	t.Run("spa fallback", func(t *testing.T) {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, "/chat/demo", nil)
		handler.ServeHTTP(recorder, request)

		if recorder.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d", recorder.Code)
		}
		if body := recorder.Body.String(); body != "index" {
			t.Fatalf("unexpected index body: %q", body)
		}
	})
}

func TestSessionLifecycle(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "test-key")

	store := session.NewStore(t.TempDir())
	handler := newApp(false, "", store, newRunManager())
	cwd := t.TempDir()

	recorder := performJSON(
		t,
		handler,
		http.MethodPost,
		"/api/sessions",
		map[string]any{
			"title":    "Test session",
			"provider": "openai",
			"cwd":      cwd,
		},
	)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var created struct {
		Session struct {
			ID    string `json:"id"`
			Title string `json:"title"`
			CWD   string `json:"cwd"`
		} `json:"session"`
	}
	decodeBody(t, recorder, &created)
	if created.Session.ID == "" {
		t.Fatal("expected session id")
	}
	if created.Session.Title != "Test session" {
		t.Fatalf("unexpected title: %s", created.Session.Title)
	}
	if created.Session.CWD != cwd {
		t.Fatalf("unexpected cwd: %s", created.Session.CWD)
	}

	recorder = httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/sessions?cwd="+cwd, nil)
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var listed struct {
		Sessions []struct {
			ID        string `json:"id"`
			Title     string `json:"title"`
			IsRunning bool   `json:"is_running"`
		} `json:"sessions"`
	}
	decodeBody(t, recorder, &listed)
	if len(listed.Sessions) != 1 || listed.Sessions[0].ID != created.Session.ID {
		t.Fatalf("unexpected sessions: %#v", listed.Sessions)
	}
	if listed.Sessions[0].IsRunning {
		t.Fatal("session should not be running")
	}

	recorder = httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodGet, "/api/sessions/"+created.Session.ID, nil)
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", recorder.Code)
	}

	var loaded struct {
		Session any `json:"session"`
	}
	decodeBody(t, recorder, &loaded)
	if loaded.Session == nil {
		t.Fatal("expected session payload")
	}
}

func TestChatRejectsMutuallyExclusiveMessageAndInput(t *testing.T) {
	handler := newApp(false, "", session.NewStore(t.TempDir()), newRunManager())

	recorder := performJSON(
		t,
		handler,
		http.MethodPost,
		"/api/chat",
		map[string]any{
			"provider": "openai",
			"message":  "hi",
			"input": []map[string]any{
				{"type": "text", "text": "hello"},
			},
		},
	)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", recorder.Code)
	}

	var payload struct {
		Detail string `json:"detail"`
	}
	decodeBody(t, recorder, &payload)
	if payload.Detail != "message and input are mutually exclusive" {
		t.Fatalf("unexpected detail: %s", payload.Detail)
	}
}

func TestBuildUserMessageEscapesAttachmentNameLikePython(t *testing.T) {
	msg, err := buildUserMessage(chatRequest{
		Input: []chatInputBlock{
			{Type: "text", Text: "print(1)", Name: `report <"draft">.py`, IsAttachment: true},
		},
	}, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if len(msg.Content) != 1 {
		t.Fatalf("unexpected message: %#v", msg)
	}
	if got := msg.Content[0].Text; got != "<file name=\"report &lt;&quot;draft&quot;&gt;.py\">\nprint(1)\n</file>" {
		t.Fatalf("unexpected attachment block: %q", got)
	}
}

func TestChatRejectsRewindForNewSessionWithoutCreatingFiles(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "test-key")

	store := session.NewStore(t.TempDir())
	handler := newApp(false, "", store, newRunManager())

	recorder := performJSON(
		t,
		handler,
		http.MethodPost,
		"/api/chat",
		map[string]any{
			"session_id": "missing",
			"message":    "retry",
			"rewind_to":  0,
			"provider":   "openai",
			"model":      "gpt-5.4",
			"cwd":        t.TempDir(),
		},
	)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var payload struct {
		Detail string `json:"detail"`
	}
	decodeBody(t, recorder, &payload)
	if payload.Detail != "rewind_to requires an existing session" {
		t.Fatalf("unexpected detail: %s", payload.Detail)
	}

	if sessions, err := store.ListSessions(""); err != nil || len(sessions) != 0 {
		t.Fatalf("unexpected sessions after rejected rewind: %#v err=%v", sessions, err)
	}
}

func TestChatRejectsRewindToCompactSummary(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "test-key")

	store := session.NewStore(t.TempDir())
	handler := newApp(false, "", store, newRunManager())
	created, err := store.CreateSession("Test", "", "openai", "gpt-5.4", "/tmp", "")
	if err != nil {
		t.Fatal(err)
	}
	sessionID := created.Session.ID

	for _, msg := range []message.Message{
		message.UserTextMessage("hello", nil),
		message.AssistantMessage([]message.Block{message.TextBlock("hi", nil)}, "openai", "gpt-5.4", "", "", nil, nil),
		session.BuildCompactEvent("summary of hello+hi", "p", "m", 2, nil),
		message.UserTextMessage("explain X", nil),
	} {
		if err := store.AppendMessage(sessionID, msg, "openai", "gpt-5.4", "/tmp", ""); err != nil {
			t.Fatal(err)
		}
	}

	recorder := performJSON(
		t,
		handler,
		http.MethodPost,
		"/api/chat",
		map[string]any{
			"session_id": sessionID,
			"message":    "retry",
			"rewind_to":  0,
			"provider":   "openai",
			"model":      "gpt-5.4",
			"cwd":        "/tmp",
		},
	)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var payload struct {
		Detail string `json:"detail"`
	}
	decodeBody(t, recorder, &payload)
	if payload.Detail != "rewind_to must reference a real user message" {
		t.Fatalf("unexpected detail: %s", payload.Detail)
	}
}

func TestConfigRoute(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "test-key")

	handler := newApp(false, "", session.NewStore(t.TempDir()), newRunManager())
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/config?cwd="+t.TempDir(), nil)
	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", recorder.Code, recorder.Body.String())
	}

	var payload struct {
		Providers map[string]map[string]any `json:"providers"`
		Default   map[string]any            `json:"default"`
	}
	decodeBody(t, recorder, &payload)
	if len(payload.Providers) == 0 {
		t.Fatal("expected at least one provider")
	}
	if _, ok := payload.Providers["openai"]; !ok {
		t.Fatalf("expected openai provider, got %#v", payload.Providers)
	}
	if payload.Default["provider"] == "" {
		t.Fatalf("expected default provider, got %#v", payload.Default)
	}
}

func performJSON(t *testing.T, handler http.Handler, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()

	data, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(method, path, bytes.NewReader(data))
	request.Header.Set("Content-Type", "application/json")
	handler.ServeHTTP(recorder, request)
	return recorder
}

func decodeBody(t *testing.T, recorder *httptest.ResponseRecorder, dst any) {
	t.Helper()
	if err := json.Unmarshal(recorder.Body.Bytes(), dst); err != nil {
		t.Fatalf("failed to decode response: %v; body=%s", err, recorder.Body.String())
	}
}
