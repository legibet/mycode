package tools

import (
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/legibet/mycode-go/internal/message"
)

const (
	// DefaultMaxLines limits read output.
	DefaultMaxLines = 2000
	// DefaultMaxBytes limits truncated tool output kept in memory.
	DefaultMaxBytes = 50 * 1024
	// ReadMaxLineChars shortens unusually long lines.
	ReadMaxLineChars = 2000
	// BashTimeout is the default bash timeout.
	BashTimeout = 120 * time.Second
	// BashMaxInMemoryBytes spills large output to disk.
	BashMaxInMemoryBytes = 5_000_000
)

// ToolSpec is the provider-facing tool definition.
type ToolSpec struct {
	Name          string         `json:"name"`
	Description   string         `json:"description"`
	InputSchema   map[string]any `json:"input_schema"`
	StreamsOutput bool           `json:"-"`
}

// Result is the canonical tool execution result.
type Result struct {
	ModelText   string          `json:"model_text"`
	DisplayText string          `json:"display_text"`
	IsError     bool            `json:"is_error"`
	Content     []message.Block `json:"content,omitempty"`
}

// Truncation reports text truncation details.
type Truncation struct {
	Truncated   bool
	TruncatedBy string
	OutputLines int
	OutputBytes int
}

// OutputCallback receives live bash output chunks.
type OutputCallback func(text string)

// Executor runs the four built-in tools for one session.
type Executor struct {
	cwd                string
	toolOutputDir      string
	supportsImageInput bool
	mu                 sync.Mutex
	activeCmds         map[*exec.Cmd]struct{}
}

// DefaultSpecs returns the four built-in tool definitions.
func DefaultSpecs() []ToolSpec {
	return []ToolSpec{
		{
			Name:        "read",
			Description: "Read a UTF-8 text file or supported image file. Returns up to 2000 lines for text files. Use offset/limit for large files. Very long lines are shortened.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path":   map[string]any{"type": "string", "description": "File path (relative or absolute)."},
					"offset": map[string]any{"type": "integer", "description": "Line number to start from (1-indexed)."},
					"limit":  map[string]any{"type": "integer", "description": "Maximum number of lines to return."},
				},
				"required":             []string{"path"},
				"additionalProperties": false,
			},
		},
		{
			Name:        "write",
			Description: "Write a file (create or overwrite).",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path":    map[string]any{"type": "string", "description": "File path (relative or absolute)."},
					"content": map[string]any{"type": "string", "description": "File content."},
				},
				"required":             []string{"path", "content"},
				"additionalProperties": false,
			},
		},
		{
			Name: "edit",
			Description: "Edit a file by replacing text snippets. " +
				"Each edits[].oldText must match uniquely in the original file. " +
				"For multiple disjoint changes in one file, use one call with multiple edits.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"path": map[string]any{"type": "string", "description": "File path (relative or absolute)."},
					"edits": map[string]any{
						"type": "array",
						"items": map[string]any{
							"type": "object",
							"properties": map[string]any{
								"oldText": map[string]any{
									"type":        "string",
									"description": "Exact text to find (must be unique in the file).",
								},
								"newText": map[string]any{
									"type":        "string",
									"description": "Replacement text.",
								},
							},
							"required":             []string{"oldText", "newText"},
							"additionalProperties": false,
						},
						"description": "Replacements to apply. All matched against the original file, not incrementally.",
					},
				},
				"required":             []string{"path", "edits"},
				"additionalProperties": false,
			},
		},
		{
			Name:          "bash",
			Description:   "Run a shell command in the session working directory. Large output returns the tail and saves the full log to a file.",
			StreamsOutput: true,
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"command": map[string]any{"type": "string", "description": "Shell command."},
					"timeout": map[string]any{"type": "integer", "description": "Timeout in seconds (optional)."},
				},
				"required":             []string{"command"},
				"additionalProperties": false,
			},
		},
	}
}

// NewExecutor creates a tool executor.
func NewExecutor(cwd, sessionDir string, supportsImageInput bool) *Executor {
	absoluteCWD := cwd
	if absoluteCWD != "" {
		if resolved, err := filepath.Abs(cwd); err == nil {
			absoluteCWD = resolved
		}
	}
	if sessionDir == "" {
		sessionDir = absoluteCWD
	}
	toolOutputDir := filepath.Join(sessionDir, "tool-output")
	_ = os.MkdirAll(toolOutputDir, 0o755)
	return &Executor{
		cwd:                absoluteCWD,
		toolOutputDir:      toolOutputDir,
		supportsImageInput: supportsImageInput,
		activeCmds:         map[*exec.Cmd]struct{}{},
	}
}

// Definitions returns provider-facing tool definitions.
func (e *Executor) Definitions() []ToolSpec {
	return DefaultSpecs()
}

// CancelActive cancels all running bash commands started by this executor.
func (e *Executor) CancelActive() {
	e.mu.Lock()
	cmds := make([]*exec.Cmd, 0, len(e.activeCmds))
	for cmd := range e.activeCmds {
		cmds = append(cmds, cmd)
	}
	e.activeCmds = map[*exec.Cmd]struct{}{}
	e.mu.Unlock()

	for _, cmd := range cmds {
		killCmdTree(cmd)
	}
}

func (e *Executor) trackCmd(cmd *exec.Cmd) {
	e.mu.Lock()
	e.activeCmds[cmd] = struct{}{}
	e.mu.Unlock()
}

func (e *Executor) untrackCmd(cmd *exec.Cmd) {
	e.mu.Lock()
	delete(e.activeCmds, cmd)
	e.mu.Unlock()
}

func killCmdTree(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err == nil {
		return
	}
	_ = cmd.Process.Kill()
}

// ParseToolArguments parses provider tool call arguments.
func ParseToolArguments(raw string) (map[string]any, error) {
	if strings.TrimSpace(raw) == "" {
		return map[string]any{}, nil
	}
	var parsed any
	if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
		return nil, err
	}
	object, ok := parsed.(map[string]any)
	if !ok {
		return nil, errors.New("tool arguments must be a JSON object")
	}
	return object, nil
}

func errorResult(modelText, displayText string) Result {
	return Result{ModelText: modelText, DisplayText: displayText, IsError: true}
}
