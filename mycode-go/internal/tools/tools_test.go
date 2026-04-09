package tools

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var png1x1 = mustBase64Decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1X8AAAAASUVORK5CYII=")

func TestTruncateText(t *testing.T) {
	t.Run("by lines", func(t *testing.T) {
		text := strings.Join([]string{
			"line 0",
			"line 1",
			"line 2",
			"line 3",
		}, "\n")
		content, trunc := TruncateText(text, 2, 1024, false)
		if content != "line 0\nline 1" {
			t.Fatalf("unexpected content: %q", content)
		}
		if !trunc.Truncated || trunc.TruncatedBy != "lines" {
			t.Fatalf("unexpected truncation: %#v", trunc)
		}
	})

	t.Run("tail", func(t *testing.T) {
		text := strings.Join([]string{
			"line 0",
			"line 1",
			"line 2",
			"line 3",
		}, "\n")
		content, trunc := TruncateText(text, 2, 1024, true)
		if content != "line 2\nline 3" {
			t.Fatalf("unexpected content: %q", content)
		}
		if !trunc.Truncated || trunc.TruncatedBy != "lines" {
			t.Fatalf("unexpected truncation: %#v", trunc)
		}
	})

	t.Run("single oversized line", func(t *testing.T) {
		content, trunc := TruncateText(strings.Repeat("x", 1000), 100, 100, false)
		if len(content) == 0 || len(content) > 100 {
			t.Fatalf("unexpected content length: %d", len(content))
		}
		if !trunc.Truncated || trunc.TruncatedBy != "bytes" || trunc.OutputLines != 1 {
			t.Fatalf("unexpected truncation: %#v", trunc)
		}
	})
}

func TestDetectImageMIMETypeFallsBackToExtension(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "tiny.png")
	if err := osWriteFile(path, nil); err != nil {
		t.Fatal(err)
	}
	if got := DetectImageMIMEType(path); got != "image/png" {
		t.Fatalf("unexpected mime type: %q", got)
	}
}

func TestRead(t *testing.T) {
	t.Run("directory", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		result := executor.Read(".", 0, 0)
		if !result.IsError || !strings.Contains(strings.ToLower(result.ModelText), "not a file") {
			t.Fatalf("unexpected result: %#v", result)
		}
	})

	t.Run("invalid utf8", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "binary.bin")
		if err := osWriteFile(path, []byte{0x80, 0x81, 0x82}); err != nil {
			t.Fatal(err)
		}
		result := executor.Read("binary.bin", 0, 0)
		if !result.IsError || !strings.Contains(strings.ToLower(result.ModelText), "utf-8") {
			t.Fatalf("unexpected result: %#v", result)
		}
	})

	t.Run("long line adds hint", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "long.txt")
		data := "short\n" + strings.Repeat("x", ReadMaxLineChars+10)
		if err := osWriteFile(path, []byte(data)); err != nil {
			t.Fatal(err)
		}
		result := executor.Read("long.txt", 0, 0)
		if !strings.Contains(result.ModelText, "... [line truncated]") {
			t.Fatalf("missing truncation marker: %q", result.ModelText)
		}
		if !strings.Contains(result.ModelText, "sed -n '2p'") {
			t.Fatalf("missing hint: %q", result.ModelText)
		}
	})

	t.Run("image content", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, true)
		path := filepath.Join(dir, "tiny.png")
		if err := osWriteFile(path, png1x1); err != nil {
			t.Fatal(err)
		}
		result := executor.Read("tiny.png", 0, 0)
		if result.IsError || result.ModelText != "Read image file [image/png]" {
			t.Fatalf("unexpected result: %#v", result)
		}
		if len(result.Content) != 2 || result.Content[1].Type != "image" {
			t.Fatalf("unexpected content: %#v", result.Content)
		}
	})
}

func TestEdit(t *testing.T) {
	t.Run("closest hint", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "test.txt")
		if err := osWriteFile(path, []byte("alpha\nbeta gamma\ndelta\n")); err != nil {
			t.Fatal(err)
		}
		result := executor.Edit("test.txt", []map[string]string{{
			"oldText": "beta gamam",
			"newText": "replacement",
		}})
		if !result.IsError || !strings.Contains(result.ModelText, "closest line: beta gamma") {
			t.Fatalf("unexpected result: %#v", result)
		}
	})

	t.Run("fuzzy trailing whitespace", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "test.py")
		if err := osWriteFile(path, []byte("def f():\n    return 1    \n")); err != nil {
			t.Fatal(err)
		}
		result := executor.Edit("test.py", []map[string]string{{
			"oldText": "def f():\n    return 1\n",
			"newText": "def f():\n    return 2\n",
		}})
		assertEditOK(t, result, []int{1}, []int{2}, []int{2})
		data, err := osReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if string(data) != "def f():\n    return 2\n" {
			t.Fatalf("unexpected file: %q", string(data))
		}
	})

	t.Run("fuzzy crlf", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "test.txt")
		if err := osWriteFile(path, []byte("line1\r\nline2\r\n")); err != nil {
			t.Fatal(err)
		}
		result := executor.Edit("test.txt", []map[string]string{{
			"oldText": "line1\nline2\n",
			"newText": "line1\nlineX\n",
		}})
		assertEditOK(t, result, []int{1}, []int{2}, []int{2})
		data, err := osReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if string(data) != "line1\r\nlineX\r\n" {
			t.Fatalf("unexpected file: %q", string(data))
		}
	})

	t.Run("normalization ambiguity", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "test.txt")
		if err := osWriteFile(path, []byte("x  \r\nx\t\r\n")); err != nil {
			t.Fatal(err)
		}
		result := executor.Edit("test.txt", []map[string]string{{
			"oldText": "x\n",
			"newText": "y\n",
		}})
		if !result.IsError || !strings.Contains(strings.ToLower(result.ModelText), "after normalization") {
			t.Fatalf("unexpected result: %#v", result)
		}
	})

	t.Run("multi edit line expansion", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		path := filepath.Join(dir, "test.txt")
		if err := osWriteFile(path, []byte("a\nb\nc\n")); err != nil {
			t.Fatal(err)
		}
		result := executor.Edit("test.txt", []map[string]string{
			{"oldText": "a", "newText": "a1\na2"},
			{"oldText": "c", "newText": "C"},
		})
		assertEditOK(t, result, []int{1, 4}, []int{1, 1}, []int{2, 1})
		data, err := osReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if string(data) != "a1\na2\nb\nC\n" {
			t.Fatalf("unexpected file: %q", string(data))
		}
	})
}

func TestBash(t *testing.T) {
	t.Run("empty output", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		result := executor.Bash("empty", "true", 0, nil)
		if result.ModelText != "(empty)" {
			t.Fatalf("unexpected result: %#v", result)
		}
	})

	t.Run("nonzero exit is not tool error", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		result := executor.Bash("exit", "echo some output; exit 42", 0, nil)
		if result.IsError {
			t.Fatalf("unexpected error result: %#v", result)
		}
		if !strings.Contains(result.ModelText, "some output") || !strings.Contains(result.ModelText, "[exit code: 42]") {
			t.Fatalf("unexpected output: %q", result.ModelText)
		}
	})

	t.Run("large output truncates and saves log", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		result := executor.Bash("large", "seq 1 3000", 0, nil)
		if !strings.Contains(result.ModelText, "Truncated:") || !strings.Contains(result.ModelText, "Full output:") {
			t.Fatalf("unexpected output: %q", result.ModelText)
		}
		if _, err := osReadFile(filepath.Join(dir, "tool-output", "bash-large.log")); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("long single line truncates by bytes", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		result := executor.Bash("long-line", "head -c 60000 /dev/zero | tr '\\000' x", 0, nil)
		if !strings.Contains(result.ModelText, "KB limit") || !strings.Contains(result.ModelText, "Full output:") {
			t.Fatalf("unexpected output: %q", result.ModelText)
		}
		if strings.Contains(result.ModelText, "0 lines") {
			t.Fatalf("unexpected output: %q", result.ModelText)
		}
	})

	t.Run("timeout", func(t *testing.T) {
		dir := t.TempDir()
		executor := NewExecutor(dir, dir, false)
		result := executor.Bash("timeout", "sleep 2", 1, nil)
		if !result.IsError || !strings.Contains(strings.ToLower(result.ModelText), "timeout") {
			t.Fatalf("unexpected result: %#v", result)
		}
	})
}

func assertEditOK(t *testing.T, result Result, startLines, oldLineCounts, newLineCounts []int) {
	t.Helper()
	var payload struct {
		Status string `json:"status"`
		Edits  []struct {
			StartLine    int `json:"start_line"`
			OldLineCount int `json:"old_line_count"`
			NewLineCount int `json:"new_line_count"`
		} `json:"edits"`
	}
	if err := json.Unmarshal([]byte(result.ModelText), &payload); err != nil {
		t.Fatalf("failed to decode payload: %v", err)
	}
	if payload.Status != "ok" || len(payload.Edits) != len(startLines) {
		t.Fatalf("unexpected payload: %#v", payload)
	}
	for i, edit := range payload.Edits {
		if edit.StartLine != startLines[i] || edit.OldLineCount != oldLineCounts[i] || edit.NewLineCount != newLineCounts[i] {
			t.Fatalf("unexpected edit %d: %#v", i, edit)
		}
	}
}

func mustBase64Decode(value string) []byte {
	data, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		panic(err)
	}
	return data
}

func osWriteFile(path string, data []byte) error {
	return os.WriteFile(path, data, 0o644)
}

func osReadFile(path string) ([]byte, error) {
	return os.ReadFile(path)
}
