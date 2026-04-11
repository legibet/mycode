package session

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
)

const (
	// MessageFormatVersion is the persisted session format version.
	MessageFormatVersion = 5
	// DefaultSessionTitle is the initial session title.
	DefaultSessionTitle = "New chat"
	// DefaultSessionProvider is the fallback provider for drafts.
	DefaultSessionProvider = "anthropic"
)

// CompactSummaryPrompt asks the model for a compact continuation summary.
const CompactSummaryPrompt = "" +
	"Summarize this conversation to create a continuation document. " +
	"This summary will replace the full conversation history, so it must " +
	"capture everything needed to continue the work seamlessly.\n\n" +
	"Include:\n\n" +
	"1. **User Requests**: Every distinct request or instruction the user gave, " +
	"in chronological order. Preserve the user's original wording for ambiguous " +
	"or nuanced requests.\n" +
	"2. **Completed Work**: What was accomplished — files created, modified, or " +
	"deleted; bugs fixed; features added. Include file paths and function names.\n" +
	"3. **Current State**: The exact state of the work right now — what is working, " +
	"what is broken, what is partially done.\n" +
	"4. **Key Decisions**: Important decisions made, constraints discovered, " +
	"approaches chosen or rejected, and why.\n" +
	"5. **Next Steps**: What remains to be done, any work that was in progress " +
	"when this summary was generated.\n\n" +
	"Rules:\n" +
	"- Be specific: include file paths, function names, error messages, and " +
	"concrete details.\n" +
	"- Do not add suggestions or opinions — only summarize what happened.\n" +
	"- Keep it concise but complete."

const compactAck = "Understood. I have the context from the conversation summary and will continue the work."

// Meta is the session metadata stored in meta.json.
type Meta struct {
	ID                   string `json:"id"`
	Title                string `json:"title"`
	Provider             string `json:"provider"`
	Model                string `json:"model"`
	CWD                  string `json:"cwd"`
	APIBase              string `json:"api_base,omitempty"`
	MessageFormatVersion int    `json:"message_format_version"`
	CreatedAt            string `json:"created_at"`
	UpdatedAt            string `json:"updated_at"`
}

// Data is one loaded session.
type Data struct {
	Session  Meta              `json:"session"`
	Messages []message.Message `json:"messages"`
}

// Store persists append-only sessions.
type Store struct {
	dataDir string
	mu      sync.Mutex
	locks   map[string]*sync.Mutex
}

// NewStore creates a session store.
func NewStore(dataDir string) *Store {
	if dataDir == "" {
		dataDir = config.ResolveSessionsDir()
	}
	_ = os.MkdirAll(dataDir, 0o755)
	return &Store{
		dataDir: dataDir,
		locks:   map[string]*sync.Mutex{},
	}
}

// ShouldCompact returns whether usage crosses the compact threshold.
func ShouldCompact(usage map[string]any, contextWindow int, threshold float64) bool {
	if len(usage) == 0 || contextWindow <= 0 || threshold <= 0 {
		return false
	}
	inputTokens := asInt(usage["input_tokens"])
	if inputTokens == 0 {
		inputTokens = asInt(usage["prompt_tokens"])
	}
	if inputTokens == 0 {
		inputTokens = asInt(usage["prompt_token_count"])
	}
	return float64(inputTokens) >= float64(contextWindow)*threshold
}

// BuildCompactEvent returns the persisted compact event.
func BuildCompactEvent(summary, provider, model string, compactedCount int, usage any) message.Message {
	meta := map[string]any{
		"provider":        provider,
		"model":           model,
		"compacted_count": compactedCount,
	}
	if usage != nil {
		meta["usage"] = usage
	}
	return message.BuildMessage("compact", []message.Block{message.TextBlock(summary, nil)}, meta)
}

// ApplyCompact replaces history before the latest compact event with a summary pair.
func ApplyCompact(messages []message.Message) []message.Message {
	lastCompact := -1
	for i, msg := range messages {
		if msg.Role == "compact" {
			lastCompact = i
		}
	}
	if lastCompact == -1 {
		return messages
	}

	summary := ""
	for _, block := range messages[lastCompact].Content {
		if block.Type == "text" {
			summary = block.Text
			break
		}
	}

	return append([]message.Message{
		message.BuildMessage("user", []message.Block{message.TextBlock("[Conversation Summary]\n\n"+summary, nil)}, map[string]any{"synthetic": true}),
		message.BuildMessage("assistant", []message.Block{message.TextBlock(compactAck, nil)}, map[string]any{"synthetic": true}),
	}, messages[lastCompact+1:]...)
}

// BuildRewindEvent returns the persisted rewind event.
func BuildRewindEvent(rewindTo int) message.Message {
	return message.Message{
		Role: "rewind",
		Meta: map[string]any{
			"rewind_to":  rewindTo,
			"created_at": now(),
		},
	}
}

// ApplyRewind truncates visible history according to rewind markers.
func ApplyRewind(messages []message.Message) []message.Message {
	out := make([]message.Message, 0, len(messages))
	for _, msg := range messages {
		if msg.Role == "rewind" {
			rewindTo := max(0, asInt(msg.Meta["rewind_to"]))
			if rewindTo < len(out) {
				out = out[:rewindTo]
			} else if rewindTo == 0 {
				out = out[:0]
			}
			continue
		}
		out = append(out, msg)
	}
	return out
}

// DraftSession builds an in-memory draft.
func (s *Store) DraftSession(title, providerName, model, cwd, apiBase string) Data {
	nowValue := now()
	return Data{
		Session: Meta{
			ID:                   newID(),
			Title:                defaultString(title, DefaultSessionTitle),
			Provider:             defaultString(providerName, DefaultSessionProvider),
			Model:                model,
			CWD:                  absPath(cwd),
			APIBase:              apiBase,
			MessageFormatVersion: MessageFormatVersion,
			CreatedAt:            nowValue,
			UpdatedAt:            nowValue,
		},
		Messages: []message.Message{},
	}
}

// CreateSession writes a session immediately.
func (s *Store) CreateSession(title, sessionID, providerName, model, cwd, apiBase string) (Data, error) {
	data := s.DraftSession(title, providerName, model, cwd, apiBase)
	if sessionID != "" {
		data.Session.ID = sessionID
	}
	lock := s.sessionLock(data.Session.ID)
	lock.Lock()
	defer lock.Unlock()

	if err := s.ensureSessionDir(data.Session.ID); err != nil {
		return Data{}, err
	}
	if err := s.writeMeta(data.Session.ID, data.Session); err != nil {
		return Data{}, err
	}
	file, err := os.OpenFile(s.messagesPath(data.Session.ID), os.O_CREATE, 0o644)
	if err == nil {
		_ = file.Close()
	}
	return data, err
}

// ListSessions returns sessions sorted by updated_at descending.
func (s *Store) ListSessions(cwd string) ([]Meta, error) {
	entries, err := os.ReadDir(s.dataDir)
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	filterCWD := absPath(cwd)
	out := make([]Meta, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		meta, err := s.readMeta(entry.Name())
		if err != nil {
			continue
		}
		if filterCWD != "" && absPath(meta.CWD) != filterCWD {
			continue
		}
		out = append(out, meta)
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].UpdatedAt > out[j].UpdatedAt
	})
	return out, nil
}

// LatestSession returns the latest session for a workspace.
func (s *Store) LatestSession(cwd string) (*Meta, error) {
	sessions, err := s.ListSessions(cwd)
	if err != nil || len(sessions) == 0 {
		return nil, err
	}
	return &sessions[0], nil
}

// LoadSession loads one session and applies replay rules.
func (s *Store) LoadSession(sessionID string) (*Data, error) {
	lock := s.sessionLock(sessionID)
	lock.Lock()
	defer lock.Unlock()

	meta, err := s.readMeta(sessionID)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	rawMessages, err := s.readMessages(sessionID)
	if err != nil {
		return nil, err
	}
	visible := ApplyRewind(ApplyCompact(rawMessages))
	if err := s.repairInterruptedToolLoop(sessionID, &meta, &visible); err != nil {
		return nil, err
	}

	return &Data{Session: meta, Messages: visible}, nil
}

// DeleteSession removes a session directory.
func (s *Store) DeleteSession(sessionID string) error {
	lock := s.sessionLock(sessionID)
	lock.Lock()
	defer lock.Unlock()
	return os.RemoveAll(s.sessionDir(sessionID))
}

// ClearSession removes persisted messages and keeps meta.
func (s *Store) ClearSession(sessionID string) error {
	lock := s.sessionLock(sessionID)
	lock.Lock()
	defer lock.Unlock()

	meta, err := s.readMeta(sessionID)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	meta.UpdatedAt = now()
	if err := s.writeMeta(sessionID, meta); err != nil {
		return err
	}
	return os.WriteFile(s.messagesPath(sessionID), nil, 0o644)
}

// AppendRewind appends a rewind event.
func (s *Store) AppendRewind(sessionID string, rewindTo int) error {
	lock := s.sessionLock(sessionID)
	lock.Lock()
	defer lock.Unlock()

	meta, err := s.readMeta(sessionID)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if err := s.ensureSessionDir(sessionID); err != nil {
		return err
	}
	if err := appendJSONL(s.messagesPath(sessionID), BuildRewindEvent(rewindTo)); err != nil {
		return err
	}
	meta.UpdatedAt = now()
	return s.writeMeta(sessionID, meta)
}

// AppendMessage appends one canonical message.
func (s *Store) AppendMessage(sessionID string, msg message.Message, providerName, model, cwd, apiBase string) error {
	lock := s.sessionLock(sessionID)
	lock.Lock()
	defer lock.Unlock()

	if err := s.ensureSessionDir(sessionID); err != nil {
		return err
	}

	meta, err := s.readMeta(sessionID)
	if err != nil {
		if !os.IsNotExist(err) {
			return err
		}
		nowValue := now()
		meta = Meta{
			ID:                   sessionID,
			Title:                DefaultSessionTitle,
			Provider:             defaultString(providerName, DefaultSessionProvider),
			Model:                model,
			CWD:                  absPath(cwd),
			APIBase:              apiBase,
			MessageFormatVersion: MessageFormatVersion,
			CreatedAt:            nowValue,
			UpdatedAt:            nowValue,
		}
	}

	if err := appendJSONL(s.messagesPath(sessionID), msg); err != nil {
		return err
	}
	meta.UpdatedAt = now()
	if meta.MessageFormatVersion == 0 {
		meta.MessageFormatVersion = MessageFormatVersion
	}
	if meta.Title == DefaultSessionTitle && msg.Role == "user" {
		if title := strings.TrimSpace(message.FlattenText(msg, false)); title != "" {
			if len(title) > 48 {
				title = title[:48]
			}
			meta.Title = title
		}
	}
	return s.writeMeta(sessionID, meta)
}

// SessionDir returns the absolute session directory.
func (s *Store) SessionDir(sessionID string) string {
	return s.sessionDir(sessionID)
}

func (s *Store) repairInterruptedToolLoop(sessionID string, meta *Meta, messages *[]message.Message) error {
	visible := *messages
	pendingIDs := []string{}
	pendingIndex := -1
	for i := len(visible) - 1; i >= 0; i-- {
		msg := visible[i]
		if msg.Role != "assistant" {
			continue
		}
		for _, block := range msg.Content {
			if block.Type == "tool_use" && block.ID != "" {
				pendingIDs = append(pendingIDs, block.ID)
			}
		}
		if len(pendingIDs) > 0 {
			pendingIndex = i
			break
		}
	}
	if pendingIndex == -1 {
		return nil
	}

	completed := map[string]struct{}{}
	for _, msg := range visible[pendingIndex+1:] {
		if msg.Role != "user" {
			continue
		}
		for _, block := range msg.Content {
			if block.Type == "tool_result" && block.ToolUseID != "" {
				completed[block.ToolUseID] = struct{}{}
			}
		}
	}

	missing := make([]string, 0, len(pendingIDs))
	for _, id := range pendingIDs {
		if _, ok := completed[id]; !ok {
			missing = append(missing, id)
		}
	}
	if len(missing) == 0 {
		return nil
	}

	repair := message.BuildMessage("user", nil, nil)
	for _, id := range missing {
		repair.Content = append(repair.Content, message.ToolResultBlock(
			id,
			"error: tool call was interrupted",
			"Tool call was interrupted",
			true,
			nil,
			nil,
		))
	}
	if err := appendJSONL(s.messagesPath(sessionID), repair); err != nil {
		return err
	}
	meta.UpdatedAt = now()
	if err := s.writeMeta(sessionID, *meta); err != nil {
		return err
	}
	*messages = append(*messages, repair)
	return nil
}

func (s *Store) readMessages(sessionID string) ([]message.Message, error) {
	file, err := os.Open(s.messagesPath(sessionID))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	defer func() {
		_ = file.Close()
	}()

	scanner := bufio.NewScanner(file)
	buffer := make([]byte, 0, 64*1024)
	scanner.Buffer(buffer, 10*1024*1024)

	out := []message.Message{}
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var msg message.Message
		if err := json.Unmarshal(line, &msg); err == nil {
			out = append(out, msg)
		}
	}
	return out, scanner.Err()
}

func (s *Store) readMeta(sessionID string) (Meta, error) {
	data, err := os.ReadFile(s.metaPath(sessionID))
	if err != nil {
		return Meta{}, err
	}
	var meta Meta
	if err := json.Unmarshal(data, &meta); err != nil {
		return Meta{}, err
	}
	return meta, nil
}

func (s *Store) writeMeta(sessionID string, meta Meta) error {
	data, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(s.metaPath(sessionID), data, 0o644)
}

func (s *Store) ensureSessionDir(sessionID string) error {
	if err := os.MkdirAll(filepath.Join(s.sessionDir(sessionID), "tool-output"), 0o755); err != nil {
		return err
	}
	return nil
}

func (s *Store) sessionDir(sessionID string) string {
	return filepath.Join(s.dataDir, sessionID)
}

func (s *Store) metaPath(sessionID string) string {
	return filepath.Join(s.sessionDir(sessionID), "meta.json")
}

func (s *Store) messagesPath(sessionID string) string {
	return filepath.Join(s.sessionDir(sessionID), "messages.jsonl")
}

func (s *Store) sessionLock(sessionID string) *sync.Mutex {
	s.mu.Lock()
	defer s.mu.Unlock()
	if lock, ok := s.locks[sessionID]; ok {
		return lock
	}
	lock := &sync.Mutex{}
	s.locks[sessionID] = lock
	return lock
}

func appendJSONL(path string, msg message.Message) error {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer func() {
		_ = file.Close()
	}()

	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	if _, err := file.Write(append(data, '\n')); err != nil {
		return err
	}
	return nil
}

func now() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func absPath(path string) string {
	if path == "" {
		return ""
	}
	value, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return value
}

func defaultString(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func asInt(value any) int {
	switch v := value.(type) {
	case int:
		return v
	case float64:
		return int(v)
	default:
		return 0
	}
}

func newID() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}
