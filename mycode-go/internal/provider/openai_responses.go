package provider

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"

	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/tools"
	"github.com/openai/openai-go/v3/responses"
)

type openAIResponsesAdapter struct {
	baseAdapter
}

func newOpenAIResponsesAdapter() Adapter {
	spec, _ := LookupSpec("openai")
	return openAIResponsesAdapter{baseAdapter: baseAdapter{spec: spec}}
}

func (a openAIResponsesAdapter) StreamTurn(ctx context.Context, req Request) <-chan StreamEvent {
	out := make(chan StreamEvent, 32)
	go func() {
		defer close(out)

		resp, err := a.openStream(ctx, req)
		if err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}
		defer func() { _ = resp.Body.Close() }()

		var final *responses.Response
		doneItems := map[int]responses.ResponseOutputItemUnion{}

		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 0, 64*1024), bufio.MaxScanTokenSize<<9)
		data := bytes.NewBuffer(nil)

		flush := func() error {
			raw := bytes.TrimSpace(append([]byte(nil), data.Bytes()...))
			data.Reset()
			if len(raw) == 0 || bytes.Equal(raw, []byte("[DONE]")) {
				return nil
			}
			return a.applyStreamEvent(raw, doneItems, &final, out)
		}

		for scanner.Scan() {
			line := scanner.Bytes()
			if len(line) == 0 {
				if err := flush(); err != nil {
					out <- StreamEvent{Type: "provider_error", Err: err}
					return
				}
				continue
			}

			name, value, ok := bytes.Cut(line, []byte(":"))
			if !ok || !bytes.Equal(name, []byte("data")) {
				continue
			}
			if len(value) > 0 && value[0] == ' ' {
				value = value[1:]
			}
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.Write(value)
		}
		if err := scanner.Err(); err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}
		if err := flush(); err != nil {
			out <- StreamEvent{Type: "provider_error", Err: err}
			return
		}
		if final == nil {
			out <- StreamEvent{Type: "provider_error", Err: fmt.Errorf("openai responses stream ended before response.completed")}
			return
		}

		keys := make([]int, 0, len(doneItems))
		for k := range doneItems {
			keys = append(keys, k)
		}
		slices.Sort(keys)
		items := make([]responses.ResponseOutputItemUnion, 0, len(keys))
		for _, idx := range keys {
			items = append(items, doneItems[idx])
		}
		msg := a.convertResponse(*final, items)
		out <- StreamEvent{Type: "message_done", Msg: &msg}
	}()
	return out
}

func (a openAIResponsesAdapter) openStream(ctx context.Context, req Request) (*http.Response, error) {
	payload := a.buildPayload(req)
	payload["stream"] = true
	bodyBytes, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}

	endpoint, err := url.JoinPath(defaultString(strings.TrimSpace(req.APIBase), a.Spec().DefaultBaseURL), "responses")
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(bodyBytes))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Authorization", "Bearer "+req.APIKey)
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")

	resp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return resp, nil
	}
	defer func() { _ = resp.Body.Close() }()

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr != nil {
		return nil, fmt.Errorf("openai responses request failed with status %s", resp.Status)
	}
	return nil, openAIResponsesHTTPError(resp.Status, raw)
}

func (a openAIResponsesAdapter) applyStreamEvent(raw []byte, doneItems map[int]responses.ResponseOutputItemUnion, final **responses.Response, out chan<- StreamEvent) error {
	var envelope struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return err
	}

	switch envelope.Type {
	case "response.reasoning_text.delta":
		var event responses.ResponseReasoningTextDeltaEvent
		if err := json.Unmarshal(raw, &event); err != nil {
			return err
		}
		if event.Delta != "" {
			out <- StreamEvent{Type: "thinking_delta", Text: event.Delta}
		}
	case "response.output_text.delta":
		var event responses.ResponseTextDeltaEvent
		if err := json.Unmarshal(raw, &event); err != nil {
			return err
		}
		if event.Delta != "" {
			out <- StreamEvent{Type: "text_delta", Text: event.Delta}
		}
	case "response.output_item.done":
		var event responses.ResponseOutputItemDoneEvent
		if err := json.Unmarshal(raw, &event); err != nil {
			return err
		}
		doneItems[int(event.OutputIndex)] = event.Item
	case "response.completed":
		var event responses.ResponseCompletedEvent
		if err := json.Unmarshal(raw, &event); err != nil {
			return err
		}
		response := event.Response
		*final = &response
	case "error":
		var event responses.ResponseErrorEvent
		if err := json.Unmarshal(raw, &event); err != nil {
			return err
		}
		return fmt.Errorf("%s", strings.TrimSpace(event.Message))
	case "response.failed":
		var event responses.ResponseFailedEvent
		if err := json.Unmarshal(raw, &event); err != nil {
			return err
		}
		msg := "response failed"
		if event.Response.Error.Message != "" {
			msg = event.Response.Error.Message
		}
		return fmt.Errorf("%s", msg)
	}
	return nil
}

func openAIResponsesHTTPError(status string, raw []byte) error {
	var body struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(raw, &body); err == nil {
		if msg := strings.TrimSpace(body.Error.Message); msg != "" {
			return fmt.Errorf("openai responses request failed with status %s: %s", status, msg)
		}
		if msg := strings.TrimSpace(body.Message); msg != "" {
			return fmt.Errorf("openai responses request failed with status %s: %s", status, msg)
		}
	}
	if text := strings.TrimSpace(string(raw)); text != "" {
		return fmt.Errorf("openai responses request failed with status %s: %s", status, text)
	}
	return fmt.Errorf("openai responses request failed with status %s", status)
}

func (a openAIResponsesAdapter) buildPayload(req Request) map[string]any {
	prepared := prepareMessages(req, defaultProjectToolCallID)
	inputItems := make([]any, 0)
	for _, msg := range prepared {
		switch msg.Role {
		case "user":
			inputItems = append(inputItems, a.serializeUserMessage(msg)...)
		case "assistant":
			if nativeItems := a.nativeOutputItems(msg); len(nativeItems) > 0 {
				inputItems = append(inputItems, nativeItems...)
				continue
			}
			inputItems = append(inputItems, a.serializeFallbackAssistantMessage(msg)...)
		}
	}

	payload := map[string]any{
		"model":             req.Model,
		"input":             inputItems,
		"store":             false,
		"include":           []string{"reasoning.encrypted_content"},
		"max_output_tokens": req.MaxTokens,
	}
	if req.System != "" {
		payload["instructions"] = req.System
	}
	if req.SessionID != "" {
		payload["prompt_cache_key"] = req.SessionID
	}
	if len(req.Tools) > 0 {
		toolsPayload := make([]any, 0, len(req.Tools))
		for _, tool := range req.Tools {
			toolsPayload = append(toolsPayload, a.serializeTool(tool))
		}
		payload["tools"] = toolsPayload
		payload["tool_choice"] = "auto"
	}
	if req.ReasoningEffort != "" {
		payload["reasoning"] = map[string]any{"effort": req.ReasoningEffort}
	}
	return payload
}

func (a openAIResponsesAdapter) serializeUserMessage(msg message.Message) []any {
	items := make([]any, 0)
	blocks := msg.Content
	messageBlocks := make([]message.Block, 0, len(blocks))
	for _, block := range blocks {
		if block.Type == "text" || block.Type == "image" || block.Type == "document" {
			messageBlocks = append(messageBlocks, block)
		}
	}
	if content := a.serializeInputContent(messageBlocks); len(content) > 0 {
		items = append(items, map[string]any{
			"type":    "message",
			"role":    "user",
			"content": content,
		})
	}

	for _, block := range blocks {
		if block.Type != "tool_result" {
			continue
		}
		resultBlocks := ToolResultContentBlocks(block)
		hasImages := false
		for _, item := range resultBlocks {
			if item.Type == "image" {
				hasImages = true
				break
			}
		}
		output := any(block.ModelText)
		if hasImages {
			output = a.serializeInputContent(resultBlocks)
		}
		items = append(items, map[string]any{
			"type":    "function_call_output",
			"call_id": block.ToolUseID,
			"output":  output,
		})
	}
	return items
}

func (a openAIResponsesAdapter) serializeInputContent(blocks []message.Block) []any {
	content := make([]any, 0, len(blocks))
	for _, block := range blocks {
		switch block.Type {
		case "text":
			content = append(content, map[string]any{"type": "input_text", "text": block.Text})
		case "image":
			mimeType, data := loadImageBlockPayload(block)
			content = append(content, map[string]any{
				"type":      "input_image",
				"image_url": "data:" + mimeType + ";base64," + data,
			})
		case "document":
			mimeType, data, name := loadDocumentBlockPayload(block)
			content = append(content, map[string]any{
				"type":      "input_file",
				"filename":  name,
				"file_data": "data:" + mimeType + ";base64," + data,
			})
		}
	}
	return content
}

func (a openAIResponsesAdapter) nativeOutputItems(msg message.Message) []any {
	if msg.Meta["provider"] != a.Spec().ID {
		return nil
	}
	outputItems, _ := messageNativeMeta(msg)["output_items"].([]any)
	if len(outputItems) == 0 {
		return nil
	}
	replay := make([]any, 0, len(outputItems))
	for _, item := range outputItems {
		copied, ok := dumpJSON(item).(map[string]any)
		if !ok {
			continue
		}
		delete(copied, "status")
		if copied["type"] != "reasoning" {
			delete(copied, "id")
		}
		replay = append(replay, copied)
	}
	return replay
}

func (a openAIResponsesAdapter) serializeFallbackAssistantMessage(msg message.Message) []any {
	items := make([]any, 0)
	textParts := make([]string, 0)
	for _, block := range msg.Content {
		if block.Type == "text" && block.Text != "" {
			textParts = append(textParts, block.Text)
		}
	}
	if len(textParts) > 0 {
		items = append(items, map[string]any{
			"type": "message",
			"role": "assistant",
			"content": []map[string]any{{
				"type": "output_text",
				"text": strings.Join(textParts, "\n"),
			}},
		})
	}
	for _, block := range msg.Content {
		if block.Type != "tool_use" {
			continue
		}
		items = append(items, map[string]any{
			"type":      "function_call",
			"call_id":   block.ID,
			"name":      block.Name,
			"arguments": mustJSON(block.Input),
		})
	}
	return items
}

func (a openAIResponsesAdapter) serializeTool(tool map[string]any) map[string]any {
	parameters := dumpJSONMap(tool["input_schema"])
	properties, _ := parameters["properties"].(map[string]any)
	requiredList, _ := parameters["required"].([]any)
	required := map[string]struct{}{}
	for _, item := range requiredList {
		if name, ok := item.(string); ok {
			required[name] = struct{}{}
		}
	}
	if properties != nil {
		copied := dumpJSONMap(properties)
		names := make([]string, 0, len(copied))
		for name := range copied {
			names = append(names, name)
		}
		slices.Sort(names)
		for name, rawSchema := range copied {
			schema, ok := rawSchema.(map[string]any)
			if !ok {
				copied[name] = map[string]any{"anyOf": []any{rawSchema, map[string]any{"type": "null"}}}
				continue
			}
			if _, ok := required[name]; ok {
				continue
			}
			switch fieldType := schema["type"].(type) {
			case string:
				schema["type"] = []any{fieldType, "null"}
			case []any:
				hasNull := false
				for _, item := range fieldType {
					if text, ok := item.(string); ok && text == "null" {
						hasNull = true
						break
					}
				}
				if !hasNull {
					schema["type"] = append(fieldType, "null")
				}
			default:
				copied[name] = map[string]any{"anyOf": []any{schema, map[string]any{"type": "null"}}}
				continue
			}
			if enumValues, ok := schema["enum"].([]any); ok {
				hasNull := false
				for _, item := range enumValues {
					if item == nil {
						hasNull = true
						break
					}
				}
				if !hasNull {
					schema["enum"] = append(enumValues, nil)
				}
			}
			copied[name] = schema
		}
		parameters["properties"] = copied
		requiredKeys := make([]any, 0, len(names))
		for _, name := range names {
			requiredKeys = append(requiredKeys, name)
		}
		parameters["required"] = requiredKeys
	}
	return map[string]any{
		"type":        "function",
		"name":        tool["name"],
		"description": tool["description"],
		"parameters":  parameters,
		"strict":      true,
	}
}

func (a openAIResponsesAdapter) convertResponse(response responses.Response, outputItems []responses.ResponseOutputItemUnion) message.Message {
	rawOutput := outputItems
	if len(rawOutput) == 0 {
		rawOutput = response.Output
	}
	blocks := make([]message.Block, 0, len(rawOutput))
	for _, item := range rawOutput {
		switch variant := item.AsAny().(type) {
		case responses.ResponseReasoningItem:
			textParts := make([]string, 0)
			for _, content := range variant.Content {
				if content.Text != "" {
					textParts = append(textParts, content.Text)
				}
			}
			if len(textParts) == 0 {
				for _, summary := range variant.Summary {
					if summary.Text != "" {
						textParts = append(textParts, summary.Text)
					}
				}
			}
			meta := map[string]any{"native": map[string]any{
				"item_id": variant.ID,
				"status":  string(variant.Status),
			}}
			if summary := dumpJSON(variant.Summary); summary != nil {
				meta["native"].(map[string]any)["summary"] = summary
			}
			blocks = append(blocks, message.ThinkingBlock(strings.Join(textParts, ""), meta))
		case responses.ResponseOutputMessage:
			for _, part := range variant.Content {
				switch content := part.AsAny().(type) {
				case responses.ResponseOutputText:
					meta := map[string]any{}
					if annotations := dumpJSON(content.Annotations); annotations != nil {
						meta["native"] = map[string]any{"annotations": annotations}
					}
					blocks = append(blocks, message.TextBlock(content.Text, meta))
				}
			}
		case responses.ResponseFunctionToolCall:
			toolInput, err := tools.ParseToolArguments(variant.Arguments)
			meta := map[string]any{"native": map[string]any{
				"item_id": variant.ID,
				"status":  string(variant.Status),
			}}
			if err != nil {
				meta["native"].(map[string]any)["raw_arguments"] = variant.Arguments
				toolInput = map[string]any{}
			}
			blocks = append(blocks, message.ToolUseBlock(variant.CallID, variant.Name, toolInput, meta))
		}
	}

	nativeMeta := map[string]any{}
	if len(rawOutput) > 0 {
		stored := make([]any, 0, len(rawOutput))
		for _, item := range rawOutput {
			var dumped any
			if err := json.Unmarshal([]byte(item.RawJSON()), &dumped); err != nil {
				continue
			}
			stored = append(stored, dumped)
		}
		if len(stored) > 0 {
			nativeMeta["output_items"] = stored
		}
	}
	return message.AssistantMessage(
		blocks,
		a.Spec().ID,
		response.Model,
		response.ID,
		string(response.Status),
		dumpJSON(response.Usage),
		nativeMeta,
	)
}

func mustJSON(value any) string {
	data, err := json.Marshal(value)
	if err != nil {
		return "{}"
	}
	return string(data)
}
