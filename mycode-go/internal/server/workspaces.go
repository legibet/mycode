package server

import (
	"io/fs"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/legibet/mycode-go/internal/workspace"
)

func (a *app) handleWorkspaceRoots(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"roots": workspace.Roots()})
}

func (a *app) handleWorkspaceBrowse(w http.ResponseWriter, r *http.Request) {
	root := strings.TrimSpace(r.URL.Query().Get("root"))
	if root == "" {
		writeDetailError(w, http.StatusBadRequest, "root is required")
		return
	}
	writeJSON(w, http.StatusOK, workspace.Browse(root, r.URL.Query().Get("path")))
}

func (a *app) handleWorkspaceCWD(w http.ResponseWriter, _ *http.Request) {
	cwd, err := os.Getwd()
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	_, statErr := os.Stat(cwd)
	writeJSON(w, http.StatusOK, map[string]any{
		"cwd":    cwd,
		"exists": statErr == nil,
	})
}

func (a *app) serveStatic(w http.ResponseWriter, r *http.Request) {
	if a.webFS == nil {
		http.NotFound(w, r)
		return
	}

	requested := path.Clean(strings.TrimPrefix(r.URL.Path, "/"))
	if requested == "." || requested == "/" {
		requested = "index.html"
	}
	if a.serveStaticFile(w, r, requested, r.URL.Path) {
		return
	}

	if a.serveStaticFile(w, r, "index.html", r.URL.Path) {
		return
	}

	http.NotFound(w, r)
}

func (a *app) serveStaticFile(w http.ResponseWriter, r *http.Request, name, requestPath string) bool {
	info, err := fs.Stat(a.webFS, name)
	if err != nil || info.IsDir() {
		return false
	}
	clone := r.Clone(r.Context())
	clone.URL.Path = requestPath
	http.ServeFileFS(w, clone, a.webFS, name)
	return true
}

func defaultWebRoot(webRoot string) string {
	if resolved := resolveWebRoot(webRoot); resolved != "" {
		return resolved
	}

	if raw := strings.TrimSpace(os.Getenv("MYCODE_WEB_DIST")); raw != "" {
		if resolved := resolveWebRoot(raw); resolved != "" {
			return resolved
		}
	}

	candidates := []string{}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates, filepath.Join(cwd, "web", "dist"))
		candidates = append(candidates, filepath.Join(cwd, "..", "web", "dist"))
	}
	if _, filename, _, ok := runtime.Caller(0); ok {
		moduleRoot := filepath.Dir(filepath.Dir(filepath.Dir(filename)))
		repoRoot := filepath.Dir(moduleRoot)
		candidates = append(candidates, filepath.Join(moduleRoot, "web", "dist"))
		candidates = append(candidates, filepath.Join(repoRoot, "web", "dist"))
	}
	for _, candidate := range candidates {
		if resolved := resolveWebRoot(candidate); resolved != "" {
			return resolved
		}
	}
	return ""
}

func resolveWebRoot(raw string) string {
	if strings.TrimSpace(raw) == "" {
		return ""
	}
	resolved := raw
	if absolute, err := filepath.Abs(raw); err == nil {
		resolved = absolute
	}
	info, err := os.Stat(resolved)
	if err != nil || !info.IsDir() {
		return ""
	}
	return resolved
}
