package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"unicode/utf8"
)

type AnthropicProvider struct {
	APIKey    string
	Endpoint  string
	Client    *http.Client
	MaxTokens int // Zero uses 8192.
}

type anthropicBlock struct {
	fields    map[string]json.RawMessage
	kind      string
	text      strings.Builder
	thinking  strings.Builder
	signature strings.Builder
	arguments strings.Builder
	open      bool
}

type anthropicMessage struct {
	Role    string            `json:"role"`
	Content []json.RawMessage `json:"content"`
}

func (p *AnthropicProvider) Stream(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
	key := p.APIKey
	if key == "" {
		key = os.Getenv("ANTHROPIC_API_KEY")
	}
	if key == "" {
		return ProviderTurn{}, errors.New("ANTHROPIC_API_KEY is not set")
	}
	endpoint := p.Endpoint
	if endpoint == "" {
		endpoint = "https://api.anthropic.com/v1/messages"
	}
	maxTokens := p.MaxTokens
	if maxTokens == 0 {
		maxTokens = 8192
	}
	if maxTokens < 0 {
		return ProviderTurn{}, errors.New("max tokens must be positive")
	}
	messages, err := anthropicInput(request.History)
	if err != nil {
		return ProviderTurn{}, err
	}
	type toolDefinition struct {
		Name        string          `json:"name"`
		Description string          `json:"description"`
		InputSchema json.RawMessage `json:"input_schema"`
	}
	tools := make([]toolDefinition, 0, len(request.Tools))
	for _, tool := range request.Tools {
		tools = append(tools, toolDefinition{tool.Name, tool.Description, tool.Parameters})
	}
	body, err := json.Marshal(struct {
		Model     string             `json:"model"`
		System    string             `json:"system,omitempty"`
		Messages  []anthropicMessage `json:"messages"`
		Tools     []toolDefinition   `json:"tools,omitempty"`
		MaxTokens int                `json:"max_tokens"`
		Stream    bool               `json:"stream"`
	}{request.Model, request.Instructions, messages, tools, maxTokens, true})
	if err != nil {
		return ProviderTurn{}, err
	}
	blocks := make(map[int]*anthropicBlock)
	var message map[string]json.RawMessage
	usage := make(map[string]json.RawMessage)
	terminal := false
	err = readSSE(ctx, p.Client, endpoint, http.Header{
		"X-Api-Key": {key}, "Anthropic-Version": {"2023-06-01"},
	}, body, func(raw json.RawMessage) (bool, error) {
		var event struct {
			Type         string                     `json:"type"`
			Index        *int                       `json:"index"`
			Message      map[string]json.RawMessage `json:"message"`
			ContentBlock json.RawMessage            `json:"content_block"`
			Delta        map[string]json.RawMessage `json:"delta"`
			Usage        map[string]json.RawMessage `json:"usage"`
			Error        struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		if err := json.Unmarshal(raw, &event); err != nil {
			return false, fmt.Errorf("invalid Anthropic event: %w", err)
		}
		if event.Type == "" {
			return false, errors.New("Anthropic event is missing its type")
		}
		switch event.Type {
		case "message_start":
			if message != nil || event.Message == nil {
				return false, errors.New("invalid or repeated Anthropic message_start")
			}
			message = event.Message
			if value, exists := message["usage"]; exists {
				if err := json.Unmarshal(value, &usage); err != nil {
					return false, err
				}
				if usage == nil {
					usage = make(map[string]json.RawMessage)
				}
			}
		case "content_block_start":
			if message == nil || event.Index == nil || *event.Index < 0 || blocks[*event.Index] != nil {
				return false, errors.New("invalid Anthropic content block index or message order")
			}
			var initial struct {
				Type      string `json:"type"`
				Text      string `json:"text"`
				Thinking  string `json:"thinking"`
				Signature string `json:"signature"`
			}
			if err := json.Unmarshal(event.ContentBlock, &initial); err != nil || initial.Type == "" {
				return false, errors.New("invalid Anthropic content block")
			}
			block := &anthropicBlock{kind: initial.Type, open: true}
			if err := json.Unmarshal(event.ContentBlock, &block.fields); err != nil {
				return false, err
			}
			block.text.WriteString(initial.Text)
			block.thinking.WriteString(initial.Thinking)
			block.signature.WriteString(initial.Signature)
			blocks[*event.Index] = block
		case "content_block_delta":
			if event.Index == nil || blocks[*event.Index] == nil || !blocks[*event.Index].open {
				return false, errors.New("Anthropic delta referenced an unopened content block")
			}
			block := blocks[*event.Index]
			var kind string
			if err := json.Unmarshal(event.Delta["type"], &kind); err != nil {
				return false, errors.New("invalid Anthropic content delta")
			}
			var field, expectedKind string
			switch kind {
			case "text_delta":
				field, expectedKind = "text", "text"
			case "thinking_delta":
				field, expectedKind = "thinking", "thinking"
			case "signature_delta":
				field, expectedKind = "signature", "thinking"
			case "input_json_delta":
				field, expectedKind = "partial_json", "tool_use"
			default:
				return false, nil
			}
			if block.kind != expectedKind {
				return false, errors.New("Anthropic delta does not match its content block type")
			}
			var value string
			if err := json.Unmarshal(event.Delta[field], &value); err != nil {
				return false, errors.New("Anthropic content delta must contain a string")
			}
			switch kind {
			case "text_delta":
				block.text.WriteString(value)
				if value != "" {
					return false, emit(value)
				}
			case "thinking_delta":
				block.thinking.WriteString(value)
			case "signature_delta":
				block.signature.WriteString(value)
			case "input_json_delta":
				block.arguments.WriteString(value)
				if block.arguments.Len() > MaxJSONLLineBytes {
					return false, errors.New("Anthropic tool arguments exceed the byte limit")
				}
			}
		case "content_block_stop":
			if event.Index == nil || blocks[*event.Index] == nil || !blocks[*event.Index].open {
				return false, errors.New("Anthropic stopped an unopened content block")
			}
			block := blocks[*event.Index]
			block.open = false
			switch block.kind {
			case "text":
				block.fields["text"], _ = json.Marshal(block.text.String())
			case "thinking":
				block.fields["thinking"], _ = json.Marshal(block.thinking.String())
				block.fields["signature"], _ = json.Marshal(block.signature.String())
			case "tool_use":
				if block.arguments.Len() > 0 {
					block.fields["input"] = json.RawMessage(block.arguments.String())
				}
				arguments := bytes.TrimSpace(block.fields["input"])
				if len(arguments) == 0 || arguments[0] != '{' || !utf8.Valid(arguments) || !json.Valid(arguments) {
					return false, errors.New("invalid tool arguments in Anthropic response")
				}
			}
		case "message_delta":
			if message == nil {
				return false, errors.New("Anthropic message_delta preceded message_start")
			}
			for key, value := range event.Delta {
				message[key] = value
			}
			for key, value := range event.Usage {
				usage[key] = value
			}
		case "message_stop":
			terminal = true
			return true, nil
		case "error":
			return false, fmt.Errorf("Anthropic stream error: %s", event.Error.Message)
		}
		return false, nil
	})
	if ctx.Err() != nil {
		return ProviderTurn{}, ctx.Err()
	}
	if err != nil {
		return ProviderTurn{}, err
	}
	if !terminal || message == nil {
		return ProviderTurn{}, errors.New("Anthropic stream ended without message_start and message_stop")
	}
	if string(message["stop_reason"]) == `"max_tokens"` {
		return ProviderTurn{}, errors.New("Anthropic response reached max_tokens before completion")
	}
	indexes := make([]int, 0, len(blocks))
	for index, block := range blocks {
		if block.open {
			return ProviderTurn{}, errors.New("Anthropic stream ended with an unfinished content block")
		}
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)
	output := make([]json.RawMessage, 0, len(indexes))
	var calls []ToolCall
	var text strings.Builder
	for _, index := range indexes {
		block := blocks[index]
		raw, err := json.Marshal(block.fields)
		if err != nil {
			return ProviderTurn{}, err
		}
		output = append(output, raw)
		if block.kind == "text" {
			text.WriteString(block.text.String())
		} else if block.kind == "tool_use" {
			var call struct {
				ID    string          `json:"id"`
				Name  string          `json:"name"`
				Input json.RawMessage `json:"input"`
			}
			if err := json.Unmarshal(raw, &call); err != nil || call.ID == "" || call.Name == "" {
				return ProviderTurn{}, errors.New("invalid tool call in Anthropic response")
			}
			calls = append(calls, ToolCall{CallID: call.ID, Name: call.Name, Arguments: call.Input})
		}
	}
	message["content"], err = json.Marshal(output)
	if err != nil {
		return ProviderTurn{}, err
	}
	message["usage"], err = json.Marshal(usage)
	if err != nil {
		return ProviderTurn{}, err
	}
	replay, err := json.Marshal(message)
	if err != nil {
		return ProviderTurn{}, err
	}
	return ProviderTurn{Text: text.String(), Output: output, ToolCalls: calls, Assistant: Message{
		Role: "assistant", Text: text.String(), ToolCalls: calls, Replay: []json.RawMessage{replay},
	}}, nil
}

func anthropicInput(history []Message) ([]anthropicMessage, error) {
	messages := make([]anthropicMessage, 0, len(history))
	for index, message := range history {
		blocks := make([]json.RawMessage, 0)
		role := message.Role
		switch message.Role {
		case "user", "assistant":
			if message.Role == "assistant" && message.Replay != nil {
				if len(message.Replay) != 1 {
					return nil, errors.New("Anthropic replay must contain one assistant message")
				}
				var replay anthropicMessage
				if err := json.Unmarshal(message.Replay[0], &replay); err != nil || replay.Content == nil {
					return nil, errors.New("invalid Anthropic replay message")
				}
				messages = append(messages, anthropicMessage{Role: "assistant", Content: replay.Content})
				continue
			}
			if message.Text != "" || message.Role == "user" {
				raw, _ := json.Marshal(struct {
					Type string `json:"type"`
					Text string `json:"text"`
				}{"text", message.Text})
				blocks = append(blocks, raw)
			}
			for _, call := range message.ToolCalls {
				raw, err := json.Marshal(struct {
					Type  string          `json:"type"`
					ID    string          `json:"id"`
					Name  string          `json:"name"`
					Input json.RawMessage `json:"input"`
				}{"tool_use", call.CallID, call.Name, call.Arguments})
				if err != nil {
					return nil, err
				}
				blocks = append(blocks, raw)
			}
		case "tool_result":
			if message.Result == nil {
				return nil, errors.New("tool_result history is missing its result")
			}
			role = "user"
			raw, _ := json.Marshal(struct {
				Type      string `json:"type"`
				ToolUseID string `json:"tool_use_id"`
				Content   string `json:"content"`
				IsError   bool   `json:"is_error"`
			}{"tool_result", message.Result.CallID, message.Result.Output, message.Result.IsError})
			if index > 0 && history[index-1].Role == "tool_result" {
				previous := &messages[len(messages)-1]
				previous.Content = append(previous.Content, raw)
				continue
			}
			blocks = append(blocks, raw)
		default:
			return nil, fmt.Errorf("unknown history role: %s", message.Role)
		}
		messages = append(messages, anthropicMessage{Role: role, Content: blocks})
	}
	return messages, nil
}
