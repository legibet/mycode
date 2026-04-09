package server

import (
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	agentpkg "github.com/legibet/mycode-go/internal/agent"
	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/provider"
)

func (a *app) handleChat(w http.ResponseWriter, r *http.Request) {
	var req chatRequest
	if err := decodeJSON(r, &req); err != nil {
		writeDetailError(w, http.StatusBadRequest, err.Error())
		return
	}
	if req.Message != "" && len(req.Input) > 0 {
		writeDetailError(w, http.StatusBadRequest, "message and input are mutually exclusive")
		return
	}

	cwd := requestCWD(req.CWD)
	settings, err := config.Load(cwd)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	resolved, err := config.ResolveProvider(
		settings,
		req.Provider,
		req.Model,
		req.APIKey,
		req.APIBase,
		req.ReasoningEffort,
	)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	userMessage, err := buildUserMessage(req, cwd)
	if err != nil {
		writeDetailError(w, http.StatusBadRequest, err.Error())
		return
	}
	if err := validateUserMessage(userMessage, resolved); err != nil {
		writeDetailError(w, http.StatusBadRequest, err.Error())
		return
	}

	sessionID := strings.TrimSpace(req.SessionID)
	if sessionID == "" {
		sessionID = "default"
	}

	data, err := a.store.LoadSession(sessionID)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	var sessionMeta any
	baseMessages := []message.Message{}
	if data != nil {
		sessionMeta = data.Session
		baseMessages = append(baseMessages, data.Messages...)
	}

	if data == nil && req.RewindTo != nil {
		writeDetailError(w, http.StatusBadRequest, "rewind_to requires an existing session")
		return
	}

	if data == nil {
		title := buildSessionTitle(userMessage)
		created, err := a.store.CreateSession(
			title,
			sessionID,
			resolved.ProviderType,
			resolved.Model,
			cwd,
			resolved.APIBase,
		)
		if err != nil {
			writeDetailError(w, http.StatusInternalServerError, err.Error())
			return
		}
		sessionMeta = created.Session
	}

	if req.RewindTo != nil {
		rewindTo := *req.RewindTo
		if rewindTo < 0 || rewindTo >= len(baseMessages) {
			writeDetailError(
				w,
				http.StatusBadRequest,
				fmt.Sprintf("rewind_to must reference a visible message index between 0 and %d", len(baseMessages)-1),
			)
			return
		}
		target := baseMessages[rewindTo]
		if !isRealUserMessage(target) {
			writeDetailError(w, http.StatusBadRequest, "rewind_to must reference a real user message")
			return
		}
		baseMessages = baseMessages[:rewindTo]
	}

	agent, err := agentpkg.New(
		resolved.Model,
		resolved.ProviderType,
		cwd,
		a.store.SessionDir(sessionID),
		sessionID,
		resolved.APIKey,
		resolved.APIBase,
		"",
		baseMessages,
		0,
		resolved.MaxTokens,
		resolved.ContextWindow,
		settings.CompactThreshold,
		resolved.ReasoningEffort,
		resolved.SupportsImageInput,
		resolved.SupportsPDFInput,
		nil,
		nil,
	)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	rewindPersisted := false
	onPersist := func(msg message.Message) error {
		if req.RewindTo != nil && !rewindPersisted {
			if err := a.store.AppendRewind(sessionID, *req.RewindTo); err != nil {
				return err
			}
			rewindPersisted = true
		}
		return a.store.AppendMessage(
			sessionID,
			msg,
			resolved.ProviderType,
			resolved.Model,
			cwd,
			resolved.APIBase,
		)
	}

	run, err := a.runs.startRun(sessionID, userMessage, baseMessages, agent, onPersist)
	if err != nil {
		if activeErr, ok := err.(activeRunError); ok {
			detail := map[string]any{"message": "session already has a running task"}
			if existing := a.runs.getRun(activeErr.RunID); existing != nil {
				detail["run"] = existing.info()
			}
			writeDetailError(w, http.StatusConflict, detail)
			return
		}
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"run":     run,
		"session": sessionMeta,
	})
}

func (a *app) handleRunStream(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	state := a.runs.getRun(runID)
	if state == nil {
		writeDetailError(w, http.StatusNotFound, "run not found")
		return
	}

	afterValue := strings.TrimSpace(r.URL.Query().Get("after"))
	after := 0
	if afterValue != "" {
		value, err := strconv.Atoi(afterValue)
		if err != nil || value < 0 {
			writeDetailError(w, http.StatusBadRequest, "after must be a non-negative integer")
			return
		}
		after = value
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeDetailError(w, http.StatusInternalServerError, "streaming is not supported")
		return
	}

	headers := w.Header()
	headers.Set("Content-Type", "text/event-stream")
	headers.Set("Cache-Control", "no-cache")
	headers.Set("Connection", "keep-alive")
	headers.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	lastSeq := after
	for {
		pending, finished := state.pendingAfter(lastSeq)
		for _, event := range pending {
			if err := writeSSE(w, event); err != nil {
				return
			}
			lastSeq = eventSeq(event, lastSeq)
			flusher.Flush()
		}

		if finished {
			_, _ = io.WriteString(w, "data: [DONE]\n\n")
			flusher.Flush()
			return
		}

		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
		}
	}
}

func (a *app) handleCancelRun(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	run := a.runs.cancelRun(runID)
	if run == nil {
		writeDetailError(w, http.StatusNotFound, "run not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"run":    run,
	})
}

func (a *app) handleConfig(w http.ResponseWriter, r *http.Request) {
	cwd := requestCWD(r.URL.Query().Get("cwd"))
	settings, err := config.Load(cwd)
	if err != nil {
		writeDetailError(w, http.StatusInternalServerError, err.Error())
		return
	}
	resolved, err := config.ResolveProvider(settings, "", "", "", "", "")
	if err != nil {
		writeDetailError(w, http.StatusServiceUnavailable, err.Error())
		return
	}

	providersInfo := map[string]any{}
	for _, choice := range config.AvailableProviders(settings) {
		spec, ok := provider.LookupSpec(choice.ProviderType)
		if !ok {
			continue
		}
		models := modelsForProvider(settings, choice)
		reasoningModels := make([]string, 0, len(models))
		imageModels := make([]string, 0, len(models))
		pdfModels := make([]string, 0, len(models))

		for _, model := range models {
			resolvedModel, err := config.ResolveProvider(
				settings,
				choice.ProviderName,
				model,
				"",
				choice.APIBase,
				"",
			)
			if err != nil {
				continue
			}
			if resolvedModel.SupportsReasoning {
				reasoningModels = append(reasoningModels, model)
			}
			if resolvedModel.SupportsImageInput {
				imageModels = append(imageModels, model)
			}
			if resolvedModel.SupportsPDFInput {
				pdfModels = append(pdfModels, model)
			}
		}

		info := map[string]any{
			"name":                 choice.ProviderName,
			"provider":             choice.ProviderType,
			"type":                 choice.ProviderType,
			"models":               models,
			"base_url":             choice.APIBase,
			"has_api_key":          true,
			"supports_image_input": bool(len(imageModels) > 0),
			"image_input_models":   imageModels,
			"supports_pdf_input":   bool(len(pdfModels) > 0),
			"pdf_input_models":     pdfModels,
		}

		if spec.SupportsReasoningEffort {
			info["supports_reasoning_effort"] = true
			info["reasoning_models"] = reasoningModels
			info["reasoning_effort"] = responseReasoningEffort(choice.ReasoningEffort)
		}

		providersInfo[choice.ProviderName] = info
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"providers":                providersInfo,
		"default":                  map[string]any{"provider": resolved.ProviderName, "model": resolved.Model},
		"default_reasoning_effort": responseReasoningEffort(settings.DefaultReasoningEffort),
		"reasoning_effort_options": reasoningEffortOptions,
		"cwd":                      cwd,
		"workspace_root":           settings.WorkspaceRoot,
		"config_paths":             settings.ConfigPaths,
	})
}
