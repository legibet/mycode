package main

import (
	"fmt"
	"io"
)

const cliName = "mycode-go"

func printUsage(w io.Writer) {
	_, _ = fmt.Fprintln(w, "Usage:")
	_, _ = fmt.Fprintf(w, "  %s run [flags] <message>\n", cliName)
	_, _ = fmt.Fprintf(w, "  %s web [flags]\n", cliName)
	_, _ = fmt.Fprintf(w, "  %s session list [--all]\n", cliName)
	_, _ = fmt.Fprintf(w, "  %s <message>\n", cliName)
	_, _ = fmt.Fprintln(w)
	_, _ = fmt.Fprintln(w, "Interactive TUI is not included in this Go rewrite.")
}

func printSessionUsage(w io.Writer) {
	_, _ = fmt.Fprintln(w, "Usage:")
	_, _ = fmt.Fprintf(w, "  %s session list [--all]\n", cliName)
}
