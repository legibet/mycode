package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"time"

	"github.com/wailsapp/wails/v2/pkg/runtime"

	"github.com/legibet/mycode-go/internal/core"
)

const (
	runEventName            = "mycode:run_event"
	desktopCommandEventName = "mycode:desktop_command"
)

type App struct {
	//nolint:containedctx // Wails provides the runtime context at startup and requires it for dialogs/events.
	ctx    context.Context
	svc    *core.Service
	svcErr error
}

type APIResult struct {
	OK     bool `json:"ok"`
	Status int  `json:"status"`
	Data   any  `json:"data,omitempty"`
	Detail any  `json:"detail,omitempty"`
}

type selectedFile struct {
	Name     string `json:"name"`
	Data     string `json:"data"`
	MIMEType string `json:"mime_type,omitempty"`
}

func NewApp() *App {
	return &App{}
}

func (a *App) startup(ctx context.Context) {
	a.ctx = ctx
	configureDesktopEnvironment()
	a.svc, a.svcErr = core.NewService(core.Options{
		Sink: func(payload core.EventPayload) {
			runtime.EventsEmit(ctx, runEventName, payload)
		},
	})
}

func (a *App) shutdown(_ context.Context) {}

func (a *App) emitDesktopCommand(command string) {
	if a.ctx == nil {
		return
	}
	runtime.EventsEmit(a.ctx, desktopCommandEventName, command)
}

func (a *App) GetConfig(cwd string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.Config(cwd)
	return result(data, err)
}

func (a *App) Settings() APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.Settings()
	return result(data, err)
}

func (a *App) UpdateSettings(raw map[string]any) APIResult {
	var req core.SettingsRequest
	if err := decode(raw, &req); err != nil {
		return result(nil, &core.StatusError{Status: 400, Detail: err.Error()})
	}
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.UpdateSettings(req)
	return result(data, err)
}

func (a *App) ListSessions(cwd string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.ListSessions(cwd)
	return result(data, err)
}

func (a *App) LoadSession(sessionID string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.LoadSession(sessionID)
	return result(data, err)
}

func (a *App) DeleteSession(sessionID string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	err = svc.DeleteSession(sessionID)
	return result(map[string]any{"status": "ok"}, err)
}

func (a *App) ClearSession(sessionID string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	err = svc.ClearSession(sessionID)
	return result(map[string]any{"status": "ok"}, err)
}

func (a *App) StartChat(raw map[string]any) APIResult {
	var req core.ChatRequest
	if err := decode(raw, &req); err != nil {
		return result(nil, &core.StatusError{Status: 400, Detail: err.Error()})
	}
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.StartChat(req)
	return result(data, err)
}

func (a *App) CancelRun(runID string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.CancelRun(runID)
	return result(data, err)
}

func (a *App) DecideRun(runID string, raw map[string]any) APIResult {
	var req core.DecideRequest
	if err := decode(raw, &req); err != nil {
		return result(nil, &core.StatusError{Status: 400, Detail: err.Error()})
	}
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.DecideRun(runID, req)
	return result(data, err)
}

func (a *App) SelectFiles(title, pattern string, multiple bool) APIResult {
	options := runtime.OpenDialogOptions{
		Title: title,
		Filters: []runtime.FileFilter{{
			DisplayName: "Supported files",
			Pattern:     pattern,
		}},
	}

	var paths []string
	if multiple {
		selected, err := runtime.OpenMultipleFilesDialog(a.ctx, options)
		if err != nil {
			return result(nil, err)
		}
		paths = selected
	} else {
		selected, err := runtime.OpenFileDialog(a.ctx, options)
		if err != nil {
			return result(nil, err)
		}
		if selected != "" {
			paths = []string{selected}
		}
	}

	files := make([]selectedFile, 0, len(paths))
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return result(nil, err)
		}
		files = append(files, selectedFile{
			Name:     filepath.Base(path),
			Data:     base64.StdEncoding.EncodeToString(data),
			MIMEType: strings.TrimSuffix(mime.TypeByExtension(filepath.Ext(path)), "; charset=utf-8"),
		})
	}
	return result(files, nil)
}

func (a *App) WorkspaceRoots() APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	return result(svc.WorkspaceRoots(), nil)
}

func (a *App) BrowseWorkspace(root, path string) APIResult {
	svc, err := a.service()
	if err != nil {
		return result(nil, err)
	}
	data, err := svc.WorkspaceBrowse(root, path)
	return result(data, err)
}

func (a *App) service() (*core.Service, error) {
	if a.svc == nil {
		a.svc, a.svcErr = core.NewService(core.Options{})
	}
	return a.svc, a.svcErr
}

func result(data any, err error) APIResult {
	if err == nil {
		return APIResult{OK: true, Status: 200, Data: data}
	}

	if statusErr, ok := errors.AsType[*core.StatusError](err); ok {
		return APIResult{
			OK:     false,
			Status: statusErr.Status,
			Detail: statusErr.Detail,
		}
	}

	return APIResult{OK: false, Status: 500, Detail: err.Error()}
}

func decode(src, dst any) error {
	data, err := json.Marshal(src)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, dst)
}

func configureDesktopEnvironment() {
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		_ = os.Chdir(home)
	}
	inheritShellEnvironment()
}

// inheritShellEnvironment repopulates os.Environ from the user's shell so
// Finder/Dock-launched desktop builds see the same development env as a
// terminal session.
func inheritShellEnvironment() bool {
	shell := desktopShell()
	for _, mode := range []string{"-il", "-l"} {
		env, ok := probeShellEnvironment(shell, mode)
		if !ok {
			continue
		}
		for key, value := range env {
			_ = os.Setenv(key, value)
		}
		return true
	}
	return false
}

func desktopShell() string {
	if shell := strings.TrimSpace(os.Getenv("SHELL")); shell != "" {
		return shell
	}
	if shell := userLoginShell(); shell != "" {
		return shell
	}
	return "/bin/sh"
}

func userLoginShell() string {
	current, err := user.Current()
	if err != nil {
		return ""
	}
	if goruntime.GOOS == "darwin" {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()

		cmd := exec.CommandContext(ctx, "dscl", ".", "-read", "/Users/"+current.Username, "UserShell")
		cmd.Stderr = io.Discard
		out, err := cmd.Output()
		if err == nil {
			return strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(string(out)), "UserShell:"))
		}
	}

	data, err := os.ReadFile("/etc/passwd")
	if err != nil {
		return ""
	}
	for line := range strings.SplitSeq(string(data), "\n") {
		parts := strings.Split(line, ":")
		if len(parts) < 7 {
			continue
		}
		if parts[0] == current.Username || parts[2] == current.Uid {
			return strings.TrimSpace(parts[6])
		}
	}
	return ""
}

func probeShellEnvironment(shell, mode string) (map[string]string, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, shell, mode, "-c", "env -0")
	cmd.Stderr = io.Discard
	out, err := cmd.Output()
	if err != nil {
		return nil, false
	}

	env := map[string]string{}
	for entry := range strings.SplitSeq(string(out), "\x00") {
		if entry == "" {
			continue
		}
		idx := strings.IndexByte(entry, '=')
		if idx <= 0 {
			continue
		}
		env[entry[:idx]] = entry[idx+1:]
	}
	return env, len(env) > 0
}
