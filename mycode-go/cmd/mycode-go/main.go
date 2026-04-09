package main

import (
	"os"
	"strings"
)

func main() {
	os.Exit(runMain(os.Args[1:]))
}

func runMain(args []string) int {
	if len(args) == 0 {
		printUsage(os.Stderr)
		return 2
	}

	switch args[0] {
	case "-h", "--help", "help":
		printUsage(os.Stdout)
		return 0
	case "run":
		return runCommand(args[1:])
	case "web":
		return webCommand(args[1:])
	case "session":
		return sessionCommand(args[1:])
	default:
		if strings.HasPrefix(args[0], "-") {
			printUsage(os.Stderr)
			return 2
		}
		return runCommand(args)
	}
}
