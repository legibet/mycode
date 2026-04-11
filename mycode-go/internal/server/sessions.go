package server

import (
	"net/http"
	"strings"

	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/session"
)

type sessionCreateRequest struct {
	Title    string `json:"title"`
	Provider string `json:"provider"`
	Model    string `json:"model"`
	CWD      string `json:"cwd"`
	APIBase  string `json:"api_base"`
}

type sessionResponse struct {
	Session       any               `json:"session"`
	Messages      []message.Message `json:"messages"`
	ActiveRun     any               `json:"active_run"`
	PendingEvents []map[string]any  `json:"pending_events"`
}

type sessionListItem struct {
	session.Meta
	IsRunning bool `json:"is_running"`
}

func (a *app) handleCreateSession(w http.ResponseWriter, r *http.Request) {
	var req sessionCreateRequest
	if err := decodeJSON(r, &req); err != nil {
		writeDetailError(w, http.StatusBadRequest, err.Error())
		return
	}

	cwd := requestCWD(req.CWD)
	settings, err := config.Load(cwd)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	resolved, err := config.ResolveProvider(settings, req.Provider, req.Model, "", req.APIBase, "")
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	data, err := a.store.CreateSession(req.Title, "", resolved.ProviderType, resolved.Model, cwd, resolved.APIBase)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, data)
}

func (a *app) handleListSessions(w http.ResponseWriter, r *http.Request) {
	cwd := strings.TrimSpace(r.URL.Query().Get("cwd"))
	items, err := a.store.ListSessions(cwd)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	sessions := make([]sessionListItem, 0, len(items))
	for _, item := range items {
		sessions = append(sessions, sessionListItem{
			Meta:      item,
			IsRunning: a.runs.hasActiveRun(item.ID),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": sessions})
}

func (a *app) handleLoadSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	data, err := a.store.LoadSession(sessionID)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	resp := sessionResponse{
		Messages:      []message.Message{},
		PendingEvents: []map[string]any{},
	}
	if data != nil {
		resp.Session = data.Session
		resp.Messages = data.Messages
	}

	if active := a.runs.snapshotSession(sessionID); active != nil {
		resp.ActiveRun = active.Run
		resp.Messages = active.Messages
		resp.PendingEvents = active.PendingEvents
	}

	writeJSON(w, http.StatusOK, resp)
}

func (a *app) handleDeleteSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if a.runs.hasActiveRun(sessionID) {
		writeDetailError(w, http.StatusConflict, "session has a running task")
		return
	}
	if err := a.store.DeleteSession(sessionID); err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

func (a *app) handleClearSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("session_id")
	if a.runs.hasActiveRun(sessionID) {
		writeDetailError(w, http.StatusConflict, "session has a running task")
		return
	}
	if err := a.store.ClearSession(sessionID); err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}
