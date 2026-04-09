package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/legibet/mycode-go/internal/session"
)

func sessionCommand(args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "session subcommand is required")
		return 2
	}

	switch args[0] {
	case "list":
		return sessionListCommand(args[1:])
	case "-h", "--help", "help":
		printSessionUsage(os.Stdout)
		return 0
	default:
		fmt.Fprintf(os.Stderr, "unknown session subcommand: %s\n", args[0])
		printSessionUsage(os.Stderr)
		return 2
	}
}

func sessionListCommand(args []string) int {
	fs := flag.NewFlagSet("session list", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	allWorkspaces := fs.Bool("all", false, "Show sessions from all workspaces")
	if err := fs.Parse(args); err != nil {
		if err == flag.ErrHelp {
			return 0
		}
		return 2
	}

	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	store := session.NewStore("")
	filterCWD := cwd
	if *allWorkspaces {
		filterCWD = ""
	}
	sessions, err := store.ListSessions(filterCWD)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	for _, item := range sessions {
		if *allWorkspaces {
			_, _ = fmt.Fprintf(os.Stdout, "%s\t%s\t%s\t%s\n", item.ID, item.Title, item.CWD, item.UpdatedAt)
			continue
		}
		_, _ = fmt.Fprintf(os.Stdout, "%s\t%s\t%s\n", item.ID, item.Title, item.UpdatedAt)
	}
	return 0
}
