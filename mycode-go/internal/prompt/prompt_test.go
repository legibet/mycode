package prompt

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInstructions(t *testing.T) {
	t.Cleanup(func() {
		userHomeDir = func() string {
			home, err := os.UserHomeDir()
			if err != nil {
				return ""
			}
			return home
		}
	})

	t.Run("prefers mycode global and current cwd only", func(t *testing.T) {
		root := t.TempDir()
		home := filepath.Join(root, "home", ".mycode")
		userHomeDir = func() string { return filepath.Join(root, "home") }
		cwd := filepath.Join(root, "project", "apps", "api")
		if err := os.MkdirAll(cwd, 0o755); err != nil {
			t.Fatal(err)
		}
		writeText(t, filepath.Join(root, "home", ".agents", "AGENTS.md"), "Global compat")
		writeText(t, filepath.Join(home, "AGENTS.md"), "Global native")
		writeText(t, filepath.Join(cwd, "AGENTS.md"), "Current cwd")

		files := discoverInstructionFiles(cwd, home)
		if len(files) != 2 {
			t.Fatalf("unexpected files: %#v", files)
		}
		if files[0] != filepath.Join(home, "AGENTS.md") || files[1] != filepath.Join(cwd, "AGENTS.md") {
			t.Fatalf("unexpected files: %#v", files)
		}

		prompt := loadInstructions(cwd, home)
		if !strings.Contains(prompt, "Global native") || !strings.Contains(prompt, "Current cwd") || strings.Contains(prompt, "Global compat") {
			t.Fatalf("unexpected prompt: %q", prompt)
		}
	})

	t.Run("does not load parent agents", func(t *testing.T) {
		root := t.TempDir()
		home := filepath.Join(root, "home", ".mycode")
		userHomeDir = func() string { return filepath.Join(root, "home") }
		cwd := filepath.Join(root, "project", "apps", "api")
		if err := os.MkdirAll(cwd, 0o755); err != nil {
			t.Fatal(err)
		}
		writeText(t, filepath.Join(root, "project", "AGENTS.md"), "Parent project")
		prompt := loadInstructions(cwd, home)
		if strings.Contains(prompt, "Parent project") {
			t.Fatalf("unexpected prompt: %q", prompt)
		}
	})

	t.Run("uses compat global when mycode missing", func(t *testing.T) {
		root := t.TempDir()
		home := filepath.Join(root, "home", ".mycode")
		userHomeDir = func() string { return filepath.Join(root, "home") }
		cwd := filepath.Join(root, "workspace")
		if err := os.MkdirAll(cwd, 0o755); err != nil {
			t.Fatal(err)
		}
		writeText(t, filepath.Join(root, "home", ".agents", "AGENTS.md"), "Compat global")

		prompt := loadInstructions(cwd, home)
		if !strings.Contains(prompt, "Compat global") {
			t.Fatalf("unexpected prompt: %q", prompt)
		}
	})
}

func TestSkills(t *testing.T) {
	t.Cleanup(func() {
		userHomeDir = func() string {
			home, err := os.UserHomeDir()
			if err != nil {
				return ""
			}
			return home
		}
	})

	t.Run("parse skill fallback name", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "SKILL.md")
		writeText(t, path, "---\ndescription: Minimal skill.\n---\nBody\n")
		skill, ok := parseSkill(path, "project", "my-tool")
		if !ok || skill.Name != "my-tool" {
			t.Fatalf("unexpected skill: %#v", skill)
		}
	})

	t.Run("scan root matches python rules", func(t *testing.T) {
		root := t.TempDir()
		writeText(t, filepath.Join(root, "deploy.md"), "---\nname: deploy\ndescription: Deploy.\n---\n")
		writeText(t, filepath.Join(root, "nested", "SKILL.md"), "---\nname: nested\ndescription: Nested.\n---\n")
		writeText(t, filepath.Join(root, "nested", "extra.md"), "---\nname: extra\ndescription: Extra.\n---\n")
		writeText(t, filepath.Join(root, ".hidden", "SKILL.md"), "---\nname: hidden\ndescription: Hidden.\n---\n")
		writeText(t, filepath.Join(root, "node_modules", "pkg", "SKILL.md"), "---\nname: pkg\ndescription: Pkg.\n---\n")
		skills := scanSkillRoot(root, "project")
		names := []string{}
		for _, skill := range skills {
			names = append(names, skill.Name)
		}
		if strings.Join(names, ",") != "deploy,nested" {
			t.Fatalf("unexpected names: %#v", names)
		}
	})

	t.Run("depth limit", func(t *testing.T) {
		root := t.TempDir()
		writeText(t, filepath.Join(root, "a", "b", "c", "SKILL.md"), "---\nname: deep-ok\ndescription: ok.\n---\n")
		writeText(t, filepath.Join(root, "a", "b", "c", "d", "SKILL.md"), "---\nname: too-deep\ndescription: nope.\n---\n")
		skills := scanSkillRoot(root, "project")
		names := []string{}
		for _, skill := range skills {
			names = append(names, skill.Name)
		}
		if len(names) != 1 || names[0] != "deep-ok" {
			t.Fatalf("unexpected names: %#v", names)
		}
	})

	t.Run("discover overrides", func(t *testing.T) {
		root := t.TempDir()
		home := filepath.Join(root, "home", ".mycode")
		userHomeDir = func() string { return filepath.Join(root, "home") }
		cwd := filepath.Join(root, "workspace")
		if err := os.MkdirAll(cwd, 0o755); err != nil {
			t.Fatal(err)
		}
		writeText(t, filepath.Join(root, "home", ".agents", "skills", "shared", "SKILL.md"), "---\nname: shared\ndescription: Compat.\n---\n")
		writeText(t, filepath.Join(home, "skills", "shared", "SKILL.md"), "---\nname: shared\ndescription: Native.\n---\n")
		writeText(t, filepath.Join(cwd, ".mycode", "skills", "shared", "SKILL.md"), "---\nname: shared\ndescription: Project.\n---\n")
		skills := discoverSkills(cwd, home)
		if len(skills) != 1 || skills[0].Description != "Project." || skills[0].Source != "project" {
			t.Fatalf("unexpected skills: %#v", skills)
		}
	})

	t.Run("load prompt format", func(t *testing.T) {
		root := t.TempDir()
		home := filepath.Join(root, "home", ".mycode")
		userHomeDir = func() string { return filepath.Join(root, "home") }
		writeText(t, filepath.Join(home, "skills", "greet", "SKILL.md"), "---\nname: greet\ndescription: Greeting skill.\n---\n")
		out := loadSkills(root, home)
		if !strings.Contains(out, "<available_skills>") || !strings.Contains(out, "name: greet") || strings.Contains(out, "Available skills:") {
			t.Fatalf("unexpected output: %q", out)
		}
	})
}

func writeText(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
