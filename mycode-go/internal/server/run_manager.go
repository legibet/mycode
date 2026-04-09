package server

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"maps"
	"sync"
	"time"

	agentpkg "github.com/legibet/mycode-go/internal/agent"
	"github.com/legibet/mycode-go/internal/message"
)

type runStatus string

const (
	runStatusRunning   runStatus = "running"
	runStatusCompleted runStatus = "completed"
	runStatusFailed    runStatus = "failed"
	runStatusCancelled runStatus = "cancelled"

	finishedRunTTL = 5 * time.Minute
)

type activeRunError struct {
	RunID string
}

func (e activeRunError) Error() string {
	return e.RunID
}

type runState struct {
	ID           string
	SessionID    string
	UserMessage  message.Message
	BaseMessages []message.Message
	Agent        *agentpkg.Agent

	mu         sync.RWMutex
	status     runStatus
	errorText  string
	events     []map[string]any
	nextSeq    int
	finishedAt time.Time
	cancel     context.CancelFunc
}

func newRunState(id, sessionID string, userMessage message.Message, baseMessages []message.Message, agent *agentpkg.Agent, cancel context.CancelFunc) *runState {
	clonedMessages := make([]message.Message, len(baseMessages))
	for i, msg := range baseMessages {
		clonedMessages[i] = message.Clone(msg)
	}
	return &runState{
		ID:           id,
		SessionID:    sessionID,
		UserMessage:  message.Clone(userMessage),
		BaseMessages: clonedMessages,
		Agent:        agent,
		status:       runStatusRunning,
		nextSeq:      1,
		cancel:       cancel,
	}
}

func (r *runState) info() map[string]any {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := map[string]any{
		"id":         r.ID,
		"session_id": r.SessionID,
		"status":     string(r.status),
		"last_seq":   r.nextSeq - 1,
	}
	if r.errorText != "" {
		out["error"] = r.errorText
	}
	return out
}

func (r *runState) appendEvent(event agentpkg.Event) {
	r.mu.Lock()
	defer r.mu.Unlock()
	payload := maps.Clone(event.Data)
	if payload == nil {
		payload = map[string]any{}
	}
	payload["seq"] = r.nextSeq
	payload["type"] = event.Type
	r.nextSeq++
	r.events = append(r.events, payload)
}

func (r *runState) finish(status runStatus, errText string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.status = status
	r.errorText = errText
	r.finishedAt = time.Now()
}

func (r *runState) pendingAfter(after int) ([]map[string]any, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	pending := make([]map[string]any, 0)
	for _, event := range r.events {
		seq, _ := event["seq"].(int)
		if seq > after {
			pending = append(pending, maps.Clone(event))
		}
	}
	return pending, r.status != runStatusRunning
}

func (r *runState) snapshot() map[string]any {
	r.mu.RLock()
	defer r.mu.RUnlock()

	messages := make([]message.Message, 0, len(r.BaseMessages)+1)
	for _, msg := range r.BaseMessages {
		messages = append(messages, message.Clone(msg))
	}
	messages = append(messages, message.Clone(r.UserMessage))
	events := make([]map[string]any, 0, len(r.events))
	for _, event := range r.events {
		events = append(events, maps.Clone(event))
	}

	run := map[string]any{
		"id":         r.ID,
		"session_id": r.SessionID,
		"status":     string(r.status),
		"last_seq":   r.nextSeq - 1,
	}
	if r.errorText != "" {
		run["error"] = r.errorText
	}
	return map[string]any{
		"run":            run,
		"messages":       messages,
		"pending_events": events,
	}
}

type runManager struct {
	mu              sync.Mutex
	activeBySession map[string]*runState
	runsByID        map[string]*runState
}

func newRunManager() *runManager {
	return &runManager{
		activeBySession: map[string]*runState{},
		runsByID:        map[string]*runState{},
	}
}

func (m *runManager) startRun(sessionID string, userMessage message.Message, baseMessages []message.Message, agent *agentpkg.Agent, onPersist func(message.Message) error) (map[string]any, error) {
	m.pruneFinishedRuns()

	m.mu.Lock()
	defer m.mu.Unlock()
	if existing := m.activeBySession[sessionID]; existing != nil {
		return nil, activeRunError{RunID: existing.ID}
	}

	ctx, cancel := context.WithCancel(context.Background())
	state := newRunState(newID(), sessionID, userMessage, baseMessages, agent, cancel)
	m.activeBySession[sessionID] = state
	m.runsByID[state.ID] = state

	go m.run(ctx, state, onPersist)
	return state.info(), nil
}

func (m *runManager) getRun(runID string) *runState {
	m.pruneFinishedRuns()
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.runsByID[runID]
}

func (m *runManager) snapshotSession(sessionID string) map[string]any {
	m.mu.Lock()
	state := m.activeBySession[sessionID]
	m.mu.Unlock()
	if state == nil {
		return nil
	}
	return state.snapshot()
}

func (m *runManager) cancelRun(runID string) map[string]any {
	state := m.getRun(runID)
	if state == nil {
		return nil
	}
	state.Agent.Cancel()
	if state.cancel != nil {
		state.cancel()
	}
	return state.info()
}

func (m *runManager) hasActiveRun(sessionID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.activeBySession[sessionID] != nil
}

func (m *runManager) run(ctx context.Context, state *runState, onPersist func(message.Message) error) {
	var lastError string
	for event := range state.Agent.Chat(ctx, state.UserMessage, onPersist) {
		if event.Type == "error" {
			if messageText, _ := event.Data["message"].(string); messageText != "" {
				lastError = messageText
			}
		}
		state.appendEvent(event)
	}

	switch lastError {
	case "cancelled":
		state.finish(runStatusCancelled, lastError)
	case "":
		state.finish(runStatusCompleted, "")
	default:
		state.finish(runStatusFailed, lastError)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if m.activeBySession[state.SessionID] == state {
		delete(m.activeBySession, state.SessionID)
	}
}

func (m *runManager) pruneFinishedRuns() {
	now := time.Now()
	m.mu.Lock()
	defer m.mu.Unlock()
	for runID, state := range m.runsByID {
		state.mu.RLock()
		finishedAt := state.finishedAt
		state.mu.RUnlock()
		if finishedAt.IsZero() {
			continue
		}
		if now.Sub(finishedAt) >= finishedRunTTL {
			delete(m.runsByID, runID)
		}
	}
}

func newID() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}
