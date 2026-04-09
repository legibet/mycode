package workspace

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBrowseRejectsPrefixEscape(t *testing.T) {
	root := t.TempDir()
	allowed := filepath.Join(root, "a")
	sibling := filepath.Join(root, "ab")
	if err := os.MkdirAll(allowed, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(sibling, 0o755); err != nil {
		t.Fatal(err)
	}

	t.Setenv("MYCODE_WORKSPACE_ROOTS", allowed)

	result := Browse(allowed, "../ab")
	if result.Error != "Path outside root" {
		t.Fatalf("unexpected result: %#v", result)
	}
}
