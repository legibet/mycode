package main

import (
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/server"
)

func webCommand(args []string) int {
	fs := flag.NewFlagSet("web", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	hostname := fs.String("hostname", "127.0.0.1", "Hostname to listen on")
	port := fs.Int("port", 0, "Port to listen on")
	dev := fs.Bool("dev", false, "Serve API only")

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
	settings, err := config.Load(cwd)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	resolvedPort := *port
	if resolvedPort <= 0 {
		resolvedPort = settings.Port
	}

	addr := net.JoinHostPort(*hostname, strconv.Itoa(resolvedPort))
	serverInstance := &http.Server{
		Addr:              addr,
		Handler:           server.NewHandler(!*dev),
		ReadHeaderTimeout: 10 * time.Second,
	}

	fmt.Fprintf(os.Stderr, "Listening on http://%s\n", addr)
	if err := serverInstance.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}
