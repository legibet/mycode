package server

import (
	"net/http"
	"strings"

	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
)

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

	sessions := make([]map[string]any, 0, len(items))
	for _, item := range items {
		sessions = append(sessions, map[string]any{
			"id":                     item.ID,
			"title":                  item.Title,
			"provider":               item.Provider,
			"model":                  item.Model,
			"cwd":                    item.CWD,
			"api_base":               item.APIBase,
			"message_format_version": item.MessageFormatVersion,
			"created_at":             item.CreatedAt,
			"updated_at":             item.UpdatedAt,
			"is_running":             a.runs.hasActiveRun(item.ID),
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

	if active := a.runs.snapshotSession(sessionID); active != nil {
		var sessionMeta any
		if data != nil {
			sessionMeta = data.Session
		}
		messages, _ := active["messages"].([]message.Message)
		pendingEvents, _ := active["pending_events"].([]map[string]any)
		writeJSON(w, http.StatusOK, sessionResponse{
			Session:       sessionMeta,
			Messages:      messages,
			ActiveRun:     active["run"],
			PendingEvents: pendingEvents,
		})
		return
	}

	if data == nil {
		writeJSON(w, http.StatusOK, sessionResponse{
			Session:       nil,
			Messages:      []message.Message{},
			ActiveRun:     nil,
			PendingEvents: []map[string]any{},
		})
		return
	}

	writeJSON(w, http.StatusOK, sessionResponse{
		Session:       data.Session,
		Messages:      data.Messages,
		ActiveRun:     nil,
		PendingEvents: []map[string]any{},
	})
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
