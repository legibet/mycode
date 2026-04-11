package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"

	agentpkg "github.com/legibet/mycode-go/internal/agent"
	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/session"
)

type resolvedSession struct {
	ID       string
	Messages []message.Message
}

func runCommand(args []string) int {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	providerName := fs.String("provider", "", "Provider id or configured alias")
	model := fs.String("model", "", "Model name")
	maxTurns := fs.Int("max-turns", 0, "Maximum tool loop turns")
	sessionID := fs.String("session", "", "Resume a specific session id")
	continueLast := fs.Bool("continue", false, "Resume the latest session in the current workspace")

	if err := fs.Parse(args); err != nil {
		if err == flag.ErrHelp {
			return 0
		}
		return 2
	}
	if *sessionID != "" && *continueLast {
		fmt.Fprintln(os.Stderr, "--session and --continue are mutually exclusive")
		return 2
	}

	prompt := strings.TrimSpace(strings.Join(fs.Args(), " "))
	if prompt == "" {
		fmt.Fprintln(os.Stderr, "message is required")
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
	resolvedProvider, err := config.ResolveProvider(settings, *providerName, *model, "", "", "")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	store := session.NewStore("")
	resolvedSession, err := resolveSession(
		store,
		resolvedProvider.ProviderType,
		cwd,
		resolvedProvider.Model,
		resolvedProvider.APIBase,
		*sessionID,
		*continueLast,
	)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	agent, err := agentpkg.New(agentpkg.Options{
		Model:              resolvedProvider.Model,
		Provider:           resolvedProvider.ProviderType,
		CWD:                cwd,
		SessionDir:         store.SessionDir(resolvedSession.ID),
		SessionID:          resolvedSession.ID,
		APIKey:             resolvedProvider.APIKey,
		APIBase:            resolvedProvider.APIBase,
		Messages:           resolvedSession.Messages,
		MaxTurns:           *maxTurns,
		MaxTokens:          resolvedProvider.MaxTokens,
		ContextWindow:      resolvedProvider.ContextWindow,
		CompactThreshold:   settings.CompactThreshold,
		ReasoningEffort:    resolvedProvider.ReasoningEffort,
		SupportsImageInput: resolvedProvider.SupportsImageInput,
		SupportsPDFInput:   resolvedProvider.SupportsPDFInput,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}

	var latestAssistant *message.Message
	onPersist := func(msg message.Message) error {
		if msg.Role == "assistant" {
			cloned := message.Clone(msg)
			latestAssistant = &cloned
		}
		return store.AppendMessage(
			resolvedSession.ID,
			msg,
			resolvedProvider.ProviderType,
			resolvedProvider.Model,
			cwd,
			resolvedProvider.APIBase,
		)
	}

	userMessage := message.UserTextMessage(prompt, nil)
	errorMessage := ""
	for event := range agent.Chat(context.Background(), userMessage, onPersist) {
		if event.Type != "error" {
			continue
		}
		if text, ok := event.Data["message"].(string); ok && text != "" {
			errorMessage = text
		} else {
			errorMessage = "agent error"
		}
	}
	if errorMessage != "" {
		fmt.Fprintln(os.Stderr, errorMessage)
		return 1
	}

	reply := flattenAssistantReply(latestAssistant)
	if reply != "" {
		_, _ = fmt.Fprintln(os.Stdout, reply)
	}
	return 0
}

func resolveSession(store *session.Store, providerType, cwd, model, apiBase, requestedSessionID string, continueLast bool) (resolvedSession, error) {
	if requestedSessionID != "" {
		data, err := store.LoadSession(requestedSessionID)
		if err != nil {
			return resolvedSession{}, err
		}
		if data == nil {
			return resolvedSession{}, fmt.Errorf("unknown session: %s", requestedSessionID)
		}
		return resolvedSession{ID: requestedSessionID, Messages: data.Messages}, nil
	}

	if continueLast {
		latest, err := store.LatestSession(cwd)
		if err != nil {
			return resolvedSession{}, err
		}
		if latest != nil && latest.ID != "" {
			data, err := store.LoadSession(latest.ID)
			if err != nil {
				return resolvedSession{}, err
			}
			if data == nil {
				return resolvedSession{}, fmt.Errorf("unknown session: %s", latest.ID)
			}
			return resolvedSession{ID: latest.ID, Messages: data.Messages}, nil
		}
	}

	draft := store.DraftSession("", providerType, model, cwd, apiBase)
	return resolvedSession{ID: draft.Session.ID, Messages: nil}, nil
}

func flattenAssistantReply(msg *message.Message) string {
	if msg == nil {
		return ""
	}
	parts := make([]string, 0, len(msg.Content))
	for _, block := range msg.Content {
		if block.Type == "text" && strings.TrimSpace(block.Text) != "" {
			parts = append(parts, block.Text)
		}
	}
	return strings.TrimSpace(strings.Join(parts, ""))
}
