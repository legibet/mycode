package server

import (
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/legibet/mycode-go/internal/config"
	"github.com/legibet/mycode-go/internal/message"
	"github.com/legibet/mycode-go/internal/provider"
	"github.com/legibet/mycode-go/internal/session"
	"github.com/legibet/mycode-go/internal/tools"
)

func buildUserMessage(req chatRequest, cwd string) (message.Message, error) {
	if len(req.Input) > 0 {
		blocks := make([]message.Block, 0, len(req.Input))
		for _, block := range req.Input {
			switch block.Type {
			case "text":
				text := block.Text
				if block.IsAttachment {
					name := strings.TrimSpace(block.Name)
					if name == "" {
						name = "attached-file"
					}
					blocks = append(blocks, message.TextBlock(
						fmt.Sprintf("<file name=\"%s\">\n%s\n</file>", escapeAttachmentAttr(name), text),
						map[string]any{"attachment": true, "path": name},
					))
					continue
				}
				if text != "" {
					blocks = append(blocks, message.TextBlock(text, nil))
				}

			case "document":
				document, err := buildDocumentBlock(block, cwd)
				if err != nil {
					return message.Message{}, err
				}
				blocks = append(blocks, document)

			case "image":
				image, err := buildImageBlock(block, cwd)
				if err != nil {
					return message.Message{}, err
				}
				blocks = append(blocks, image)

			default:
				return message.Message{}, fmt.Errorf("unsupported input block type: %s", block.Type)
			}
		}
		if len(blocks) == 0 {
			return message.Message{}, fmt.Errorf("input must include at least one non-empty block")
		}
		return message.BuildMessage("user", blocks, nil), nil
	}

	text := strings.TrimSpace(req.Message)
	if text == "" {
		return message.Message{}, fmt.Errorf("message or input is required")
	}
	return message.UserTextMessage(text, nil), nil
}

func buildImageBlock(block chatInputBlock, cwd string) (message.Block, error) {
	if block.Data != "" {
		if strings.TrimSpace(block.MIMEType) == "" {
			return message.Block{}, fmt.Errorf("image data requires mime_type")
		}
		return message.ImageBlock(block.Data, block.MIMEType, defaultString(block.Name, "image"), nil), nil
	}
	if strings.TrimSpace(block.Path) == "" {
		return message.Block{}, fmt.Errorf("image input requires path or data")
	}

	resolvedPath := tools.ResolvePath(block.Path, cwd)
	info, err := os.Stat(resolvedPath)
	if err != nil || info.IsDir() {
		return message.Block{}, fmt.Errorf("image file not found: %s", block.Path)
	}
	mimeType := strings.TrimSpace(block.MIMEType)
	if mimeType == "" {
		mimeType = tools.DetectImageMIMEType(resolvedPath)
	}
	if mimeType == "" {
		return message.Block{}, fmt.Errorf("unsupported image file: %s", block.Path)
	}

	data, err := os.ReadFile(resolvedPath)
	if err != nil {
		return message.Block{}, fmt.Errorf("failed to read image file: %w", err)
	}
	return message.ImageBlock(
		base64.StdEncoding.EncodeToString(data),
		mimeType,
		defaultString(block.Name, filepath.Base(resolvedPath)),
		nil,
	), nil
}

func buildDocumentBlock(block chatInputBlock, cwd string) (message.Block, error) {
	if block.Data != "" {
		mimeType := defaultString(block.MIMEType, "application/pdf")
		if mimeType != "application/pdf" {
			return message.Block{}, fmt.Errorf("unsupported document mime_type")
		}
		return message.DocumentBlock(block.Data, mimeType, defaultString(block.Name, "document.pdf"), nil), nil
	}
	if strings.TrimSpace(block.Path) == "" {
		return message.Block{}, fmt.Errorf("document input requires path or data")
	}

	resolvedPath := tools.ResolvePath(block.Path, cwd)
	info, err := os.Stat(resolvedPath)
	if err != nil || info.IsDir() {
		return message.Block{}, fmt.Errorf("document file not found: %s", block.Path)
	}
	mimeType := strings.TrimSpace(block.MIMEType)
	if mimeType == "" {
		mimeType = tools.DetectDocumentMIMEType(resolvedPath)
	}
	if mimeType != "application/pdf" {
		return message.Block{}, fmt.Errorf("unsupported document file: %s", block.Path)
	}

	data, err := os.ReadFile(resolvedPath)
	if err != nil {
		return message.Block{}, fmt.Errorf("failed to read document file: %w", err)
	}
	return message.DocumentBlock(
		base64.StdEncoding.EncodeToString(data),
		mimeType,
		defaultString(block.Name, filepath.Base(resolvedPath)),
		nil,
	), nil
}

func validateUserMessage(userMessage message.Message, resolved config.ResolvedProvider) error {
	for _, block := range userMessage.Content {
		switch block.Type {
		case "image":
			if !resolved.SupportsImageInput {
				return fmt.Errorf("current model does not support image input")
			}
		case "document":
			if !resolved.SupportsPDFInput {
				return fmt.Errorf("current model does not support PDF input")
			}
		}
	}
	return nil
}

func buildSessionTitle(msg message.Message) string {
	title := strings.TrimSpace(strings.ReplaceAll(message.FlattenText(msg, false), "\n", " "))
	if title == "" {
		return session.DefaultSessionTitle
	}
	if len(title) <= 48 {
		return title
	}
	return strings.TrimSpace(title[:48])
}

func escapeAttachmentAttr(value string) string {
	return strings.NewReplacer(
		"&", "&amp;",
		`"`, "&quot;",
		"<", "&lt;",
		">", "&gt;",
	).Replace(value)
}

func isRealUserMessage(msg message.Message) bool {
	if msg.Role != "user" {
		return false
	}
	if msg.Meta["synthetic"] == true {
		return false
	}
	for _, block := range msg.Content {
		if block.Type == "image" || block.Type == "document" {
			return true
		}
		if block.Type == "text" && strings.TrimSpace(block.Text) != "" {
			return true
		}
	}
	return false
}

func modelsForProvider(settings config.Settings, resolved config.ResolvedProvider) []string {
	if providerConfig, ok := settings.Providers[resolved.ProviderName]; ok && len(providerConfig.Models) > 0 {
		models := append([]string(nil), providerConfig.ModelOrder...)
		if len(models) == 0 {
			for model := range providerConfig.Models {
				models = append(models, model)
			}
			sort.Strings(models)
		}
		return models
	}
	spec, ok := provider.LookupSpec(resolved.ProviderType)
	if !ok {
		return []string{resolved.Model}
	}
	models := append([]string(nil), spec.DefaultModels...)
	if len(models) == 0 {
		models = []string{resolved.Model}
	}
	return models
}
