package server

import (
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/legibet/mycode-go/internal/session"
)

var reasoningEffortOptions = []string{"auto", "none", "low", "medium", "high", "xhigh"}

type app struct {
	store    *session.Store
	runs     *runManager
	serveWeb bool
	webRoot  string
	webFS    fs.FS
	api      *http.ServeMux
}

// NewHandler builds the HTTP handler for the API and optional web UI.
func NewHandler(serveWeb bool) http.Handler {
	return newApp(serveWeb, "", nil, nil)
}

func newApp(serveWeb bool, webRoot string, store *session.Store, runs *runManager) *app {
	if store == nil {
		store = session.NewStore("")
	}
	if runs == nil {
		runs = newRunManager()
	}
	resolvedWebRoot := webRoot
	var webFS fs.FS
	if serveWeb {
		resolvedWebRoot = defaultWebRoot(resolvedWebRoot)
		if resolvedWebRoot != "" {
			webFS = os.DirFS(resolvedWebRoot)
		} else {
			webFS = embeddedWebFS()
		}
	}

	mux := http.NewServeMux()
	app := &app{
		store:    store,
		runs:     runs,
		serveWeb: serveWeb,
		webRoot:  resolvedWebRoot,
		webFS:    webFS,
		api:      mux,
	}

	mux.HandleFunc("POST /api/chat", app.handleChat)
	mux.HandleFunc("GET /api/runs/{run_id}/stream", app.handleRunStream)
	mux.HandleFunc("POST /api/runs/{run_id}/cancel", app.handleCancelRun)
	mux.HandleFunc("GET /api/config", app.handleConfig)

	mux.HandleFunc("POST /api/sessions", app.handleCreateSession)
	mux.HandleFunc("GET /api/sessions", app.handleListSessions)
	mux.HandleFunc("GET /api/sessions/{session_id}", app.handleLoadSession)
	mux.HandleFunc("DELETE /api/sessions/{session_id}", app.handleDeleteSession)
	mux.HandleFunc("POST /api/sessions/{session_id}/clear", app.handleClearSession)

	mux.HandleFunc("GET /api/workspaces/roots", app.handleWorkspaceRoots)
	mux.HandleFunc("GET /api/workspaces/browse", app.handleWorkspaceBrowse)
	mux.HandleFunc("GET /api/workspaces/cwd", app.handleWorkspaceCWD)

	return app
}

func (a *app) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	setCORSHeaders(w)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/") || r.URL.Path == "/api" {
		a.api.ServeHTTP(w, r)
		return
	}
	if !a.serveWeb {
		http.NotFound(w, r)
		return
	}
	a.serveStatic(w, r)
}

// HTTP helpers used across all handlers.

func decodeJSON(r *http.Request, dst any) error {
	defer func() { _ = r.Body.Close() }()
	if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
		return fmt.Errorf("invalid JSON body: %w", err)
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
}

func writeDetailError(w http.ResponseWriter, status int, detail any) {
	writeJSON(w, status, map[string]any{"detail": detail})
}

func writeSSE(w io.Writer, payload any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "data: %s\n\n", data)
	return err
}

func setCORSHeaders(w http.ResponseWriter) {
	headers := w.Header()
	headers.Set("Access-Control-Allow-Origin", "*")
	headers.Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
	headers.Set("Access-Control-Allow-Headers", "*")
}

func eventSeq(event map[string]any, fallback int) int {
	switch v := event["seq"].(type) {
	case int:
		return v
	case int64:
		return int(v)
	case float64:
		return int(v)
	default:
		return fallback
	}
}

func requestCWD(value string) string {
	resolved := strings.TrimSpace(value)
	if resolved == "" {
		cwd, err := os.Getwd()
		if err != nil {
			return "."
		}
		resolved = cwd
	}
	if absolute, err := filepath.Abs(resolved); err == nil {
		return absolute
	}
	return resolved
}

func responseReasoningEffort(value string) string {
	if strings.TrimSpace(value) == "" {
		return "auto"
	}
	return value
}

func defaultString(value, fallback string) string {
	if strings.TrimSpace(value) == "" {
		return fallback
	}
	return value
}
