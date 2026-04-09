package workspace

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// BrowseEntry is one browse result.
type BrowseEntry struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// BrowseResult is the workspace browse response.
type BrowseResult struct {
	Root    string        `json:"root"`
	Path    string        `json:"path"`
	Current string        `json:"current"`
	Entries []BrowseEntry `json:"entries"`
	Error   string        `json:"error"`
}

// Roots returns allowed workspace roots.
func Roots() []string {
	raw := strings.TrimSpace(os.Getenv("MYCODE_WORKSPACE_ROOTS"))
	if raw == "" {
		raw = strings.TrimSpace(os.Getenv("WORKSPACE_ROOTS"))
	}
	var values []string
	if raw != "" {
		values = strings.Split(raw, ",")
	} else {
		home, _ := os.UserHomeDir()
		values = []string{home, string(filepath.Separator)}
	}

	seen := map[string]struct{}{}
	out := []string{}
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		resolved, err := filepath.Abs(value)
		if err != nil {
			continue
		}
		if _, err := os.Stat(resolved); err != nil {
			continue
		}
		if _, ok := seen[resolved]; ok {
			continue
		}
		seen[resolved] = struct{}{}
		out = append(out, resolved)
	}
	if len(out) == 0 {
		cwd, _ := os.Getwd()
		out = []string{cwd}
	}
	return out
}

// Browse returns directories within an allowed root.
func Browse(root, relativePath string) BrowseResult {
	requestedRoot, err := filepath.Abs(root)
	if err != nil {
		return BrowseResult{Root: root, Error: "Invalid root"}
	}
	allowed := ""
	for _, candidate := range Roots() {
		if requestedRoot == candidate {
			allowed = candidate
			break
		}
	}
	if allowed == "" {
		return BrowseResult{Root: root, Error: "Invalid root"}
	}

	target := filepath.Join(allowed, relativePath)
	target, err = filepath.Abs(target)
	if err != nil {
		return BrowseResult{
			Root:    allowed,
			Current: allowed,
			Error:   err.Error(),
		}
	}
	if !withinRoot(allowed, target) {
		return BrowseResult{
			Root:    allowed,
			Current: allowed,
			Error:   "Path outside root",
		}
	}

	entries, err := os.ReadDir(target)
	if err != nil {
		return BrowseResult{
			Root:    allowed,
			Current: allowed,
			Error:   err.Error(),
		}
	}
	out := make([]BrowseEntry, 0, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() || strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		fullPath := filepath.Join(target, entry.Name())
		rel, err := filepath.Rel(allowed, fullPath)
		if err != nil {
			continue
		}
		out = append(out, BrowseEntry{Name: entry.Name(), Path: filepath.ToSlash(rel)})
	}
	sort.Slice(out, func(i, j int) bool {
		return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name)
	})

	relCurrent := ""
	if target != allowed {
		relCurrent, err = filepath.Rel(allowed, target)
		if err != nil {
			relCurrent = ""
		}
		relCurrent = filepath.ToSlash(relCurrent)
	}
	return BrowseResult{
		Root:    allowed,
		Path:    relCurrent,
		Current: target,
		Entries: out,
	}
}

func withinRoot(root, target string) bool {
	rel, err := filepath.Rel(root, target)
	if err != nil {
		return false
	}
	if rel == ".." {
		return false
	}
	return !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
