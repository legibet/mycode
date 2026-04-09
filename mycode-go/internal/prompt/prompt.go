package prompt

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	maxScanDepth   = 3
	maxDirsPerRoot = 200
	nameMaxLen     = 64
)

var (
	skipDirs = map[string]struct{}{
		"node_modules": {},
		"__pycache__":  {},
		".git":         {},
	}
	namePattern = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)
	userHomeDir = func() string {
		home, err := os.UserHomeDir()
		if err != nil {
			return ""
		}
		return home
	}
)

const basePrompt = "" +
	"You are mycode, an expert coding assistant.\n\n" +
	"You have four tools: read, write, edit, bash.\n\n" +
	"- Use bash for file operations and exploration like `ls`, `find`, `rg`, etc.\n" +
	"- Always set offset/limit when reading large files.\n" +
	"- Always read files before editing them.\n" +
	"- Use write only for new files or complete rewrites\n" +
	"- Your response should be concise and relevant.\n" +
	"- When available skills match the current task, prefer them over manual alternatives. To use a skill: read its `SKILL.md`, then follow the instructions inside."

// Skill is the discovered skill summary injected into the system prompt.
type Skill struct {
	Name        string
	Description string
	Path        string
	Source      string
}

// Build returns the runtime system prompt.
func Build(cwd, home string) string {
	resolvedCWD := absPath(cwd)
	parts := []string{basePrompt}
	if instructions := loadInstructions(resolvedCWD, home); instructions != "" {
		parts = append(parts, instructions)
	}
	if skills := loadSkills(resolvedCWD, home); skills != "" {
		parts = append(parts, skills)
	}
	parts = append(parts, "Current working directory: "+resolvedCWD)
	return strings.Join(parts, "\n\n")
}

func loadInstructions(cwd, home string) string {
	sections := []string{}
	for _, path := range discoverInstructionFiles(cwd, home) {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		text := strings.TrimSpace(string(data))
		if text == "" {
			continue
		}
		sections = append(sections, fmt.Sprintf("## %s\n%s", path, text))
	}
	if len(sections) == 0 {
		return ""
	}
	return "<workspace_instructions>\n" +
		"Instructions are ordered from global to current cwd. Later files are more specific.\n\n" +
		strings.Join(sections, "\n\n") +
		"\n</workspace_instructions>"
}

func discoverInstructionFiles(cwd, home string) []string {
	resolvedCWD := absPath(cwd)
	resolvedHome := absPath(home)
	files := []string{}

	globalCandidate := filepath.Join(resolvedHome, "AGENTS.md")
	compatCandidate := filepath.Join(absPath(userHomeDir()), ".agents", "AGENTS.md")
	if isFile(globalCandidate) {
		files = append(files, globalCandidate)
	} else if isFile(compatCandidate) {
		files = append(files, compatCandidate)
	}

	localCandidate := filepath.Join(resolvedCWD, "AGENTS.md")
	if isFile(localCandidate) {
		files = append(files, localCandidate)
	}
	return files
}

func loadSkills(cwd, home string) string {
	skills := discoverSkills(cwd, home)
	if len(skills) == 0 {
		return ""
	}
	lines := []string{"<available_skills>"}
	for _, skill := range skills {
		lines = append(lines, "- name: "+skill.Name)
		lines = append(lines, "  path: "+skill.Path)
		lines = append(lines, "  description: "+skill.Description)
		lines = append(lines, "")
	}
	lines = append(lines, "</available_skills>")
	return strings.Join(lines, "\n")
}

func discoverSkills(cwd, home string) []Skill {
	cwdPath := absPath(cwd)
	homePath := absPath(home)
	compatHome := absPath(userHomeDir())
	roots := []struct {
		path   string
		source string
	}{
		{filepath.Join(compatHome, ".agents", "skills"), "global"},
		{filepath.Join(homePath, "skills"), "global"},
		{filepath.Join(cwdPath, ".agents", "skills"), "project"},
		{filepath.Join(cwdPath, ".mycode", "skills"), "project"},
	}

	skillsByName := map[string]Skill{}
	seenPaths := map[string]struct{}{}
	for _, root := range roots {
		for _, skill := range scanSkillRoot(root.path, root.source) {
			if _, ok := seenPaths[skill.Path]; ok {
				continue
			}
			seenPaths[skill.Path] = struct{}{}
			if prev, ok := skillsByName[skill.Name]; ok {
				slog.Debug("skill overridden", "name", skill.Name, "new", skill.Path, "prev", prev.Path)
			}
			skillsByName[skill.Name] = skill
		}
	}

	names := make([]string, 0, len(skillsByName))
	for name := range skillsByName {
		names = append(names, name)
	}
	slices.Sort(names)

	out := make([]Skill, 0, len(names))
	for _, name := range names {
		out = append(out, skillsByName[name])
	}
	return out
}

func scanSkillRoot(root, source string) []Skill {
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return nil
	}

	rootEntries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	slices.SortFunc(rootEntries, func(a, b os.DirEntry) int {
		return strings.Compare(a.Name(), b.Name())
	})

	skills := []Skill{}
	seenPaths := map[string]struct{}{}
	for _, entry := range rootEntries {
		if entry.Type().IsRegular() && filepath.Ext(entry.Name()) == ".md" {
			path := filepath.Join(root, entry.Name())
			skill, ok := parseSkill(path, source, strings.TrimSuffix(entry.Name(), filepath.Ext(entry.Name())))
			if ok {
				if _, seen := seenPaths[skill.Path]; !seen {
					seenPaths[skill.Path] = struct{}{}
					skills = append(skills, skill)
				}
			}
		}
	}

	type pendingDir struct {
		path  string
		depth int
	}
	queue := []pendingDir{}
	for _, entry := range rootEntries {
		if !entry.IsDir() {
			continue
		}
		if strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		if _, skip := skipDirs[entry.Name()]; skip {
			continue
		}
		info, err := entry.Info()
		if err != nil || info.Mode()&os.ModeSymlink != 0 {
			continue
		}
		queue = append(queue, pendingDir{path: filepath.Join(root, entry.Name()), depth: 1})
	}

	dirsScanned := 0
	for len(queue) > 0 && dirsScanned < maxDirsPerRoot {
		current := queue[0]
		queue = queue[1:]
		dirsScanned++

		skillPath := filepath.Join(current.path, "SKILL.md")
		if skill, ok := parseSkill(skillPath, source, filepath.Base(current.path)); ok {
			if _, seen := seenPaths[skill.Path]; !seen {
				seenPaths[skill.Path] = struct{}{}
				skills = append(skills, skill)
			}
		}

		if current.depth >= maxScanDepth {
			continue
		}

		children, err := os.ReadDir(current.path)
		if err != nil {
			continue
		}
		slices.SortFunc(children, func(a, b os.DirEntry) int {
			return strings.Compare(a.Name(), b.Name())
		})
		for _, child := range children {
			if !child.IsDir() {
				continue
			}
			if strings.HasPrefix(child.Name(), ".") {
				continue
			}
			if _, skip := skipDirs[child.Name()]; skip {
				continue
			}
			info, err := child.Info()
			if err != nil || info.Mode()&os.ModeSymlink != 0 {
				continue
			}
			queue = append(queue, pendingDir{
				path:  filepath.Join(current.path, child.Name()),
				depth: current.depth + 1,
			})
		}
	}

	return skills
}

func parseSkill(path, source, fallbackName string) (Skill, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Skill{}, false
	}

	frontmatter, ok := parseFrontmatter(string(data))
	if !ok {
		return Skill{}, false
	}

	name := ""
	for _, candidate := range []string{asString(frontmatter["name"]), fallbackName} {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" || len(candidate) > nameMaxLen || !namePattern.MatchString(candidate) {
			continue
		}
		name = candidate
		break
	}
	description := strings.TrimSpace(asString(frontmatter["description"]))
	if name == "" || description == "" {
		return Skill{}, false
	}
	return Skill{
		Name:        name,
		Description: description,
		Path:        absPath(path),
		Source:      source,
	}, true
}

func parseFrontmatter(text string) (map[string]any, bool) {
	lines := strings.Split(text, "\n")
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return nil, false
	}
	end := -1
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			end = i
			break
		}
	}
	if end == -1 {
		return nil, false
	}

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(strings.Join(lines[1:end], "\n")), &parsed); err != nil {
		return nil, false
	}
	if parsed == nil {
		return nil, false
	}
	return parsed, true
}

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func absPath(path string) string {
	if strings.HasPrefix(path, "~/") {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, strings.TrimPrefix(path, "~/"))
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return filepath.Clean(path)
	}
	return filepath.Clean(absolute)
}

func asString(value any) string {
	text, _ := value.(string)
	return text
}
