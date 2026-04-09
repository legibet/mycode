package server

import "github.com/legibet/mycode-go/internal/message"

type chatInputBlock struct {
	Type         string `json:"type"`
	Text         string `json:"text"`
	Path         string `json:"path"`
	Data         string `json:"data"`
	MIMEType     string `json:"mime_type"`
	Name         string `json:"name"`
	IsAttachment bool   `json:"is_attachment"`
}

type chatRequest struct {
	SessionID       string           `json:"session_id"`
	Message         string           `json:"message"`
	Input           []chatInputBlock `json:"input"`
	Provider        string           `json:"provider"`
	Model           string           `json:"model"`
	CWD             string           `json:"cwd"`
	APIKey          string           `json:"api_key"`
	APIBase         string           `json:"api_base"`
	ReasoningEffort string           `json:"reasoning_effort"`
	RewindTo        *int             `json:"rewind_to"`
}

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
