package server

import (
	"io/fs"
	"net/http"
	"os"
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
		switch {
		case resolvedWebRoot != "":
			webFS = os.DirFS(resolvedWebRoot)
		default:
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
