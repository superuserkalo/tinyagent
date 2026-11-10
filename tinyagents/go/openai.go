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

// OpenAIProvider uses the Responses API. Empty fields use the public endpoint,
// OPENAI_API_KEY, and the default HTTP client with a three-minute timeout.
type OpenAIProvider struct {
	APIKey   string
	Endpoint string
	Client   *http.Client
}

type openAIItem struct {
	Type      string `json:"type"`
	CallID    string `json:"call_id"`
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
	Content   []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
}

func (p *OpenAIProvider) Stream(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
	key := p.APIKey
	if key == "" {
		key = os.Getenv("OPENAI_API_KEY")
	}
	if key == "" {
		return ProviderTurn{}, errors.New("OPENAI_API_KEY is not set")
	}
	endpoint := p.Endpoint
	if endpoint == "" {
		endpoint = "https://api.openai.com/v1/responses"
	}
	input, err := openAIInput(request.History)
	if err != nil {
		return ProviderTurn{}, err
	}
	type function struct {
		Type string `json:"type"`
		ToolDefinition
		Strict bool `json:"strict"`
	}
	tools := make([]function, 0, len(request.Tools))
	for _, tool := range request.Tools {
		tools = append(tools, function{Type: "function", ToolDefinition: tool, Strict: true})
	}
	body, err := json.Marshal(struct {
		Model             string            `json:"model"`
		Instructions      string            `json:"instructions"`
		Input             []json.RawMessage `json:"input"`
		Tools             []function        `json:"tools"`
		ParallelToolCalls bool              `json:"parallel_tool_calls"`
		Store             bool              `json:"store"`
		Stream            bool              `json:"stream"`
		Include           []string          `json:"include"`
	}{
		Model: request.Model, Instructions: request.Instructions, Input: input, Tools: tools,
		ParallelToolCalls: false, Store: false, Stream: true,
		Include: []string{"reasoning.encrypted_content"},
	})
	if err != nil {
		return ProviderTurn{}, err
	}
	items := make(map[int]json.RawMessage)
	arguments := make(map[int]string)
	var output []json.RawMessage
	terminal := false
	err = readSSE(ctx, p.Client, endpoint, http.Header{"Authorization": {"Bearer " + key}}, body,
		func(raw json.RawMessage) (bool, error) {
			var event struct {
				Type        string          `json:"type"`
				OutputIndex *int            `json:"output_index"`
				Item        json.RawMessage `json:"item"`
				Delta       string          `json:"delta"`
				Arguments   string          `json:"arguments"`
				Message     string          `json:"message"`
				Response    struct {
					Output *[]json.RawMessage `json:"output"`
					Error  struct {
						Message string `json:"message"`
					} `json:"error"`
				} `json:"response"`
			}
			if err := json.Unmarshal(raw, &event); err != nil {
				return false, fmt.Errorf("invalid OpenAI event: %w", err)
			}
			if event.Type == "" {
				return false, errors.New("OpenAI event is missing its type")
			}
			switch event.Type {
			case "response.output_item.added", "response.output_item.done":
				if event.OutputIndex == nil || len(event.Item) == 0 {
					return false, errors.New("OpenAI output item is missing its index or item")
				}
				items[*event.OutputIndex] = bytes.Clone(event.Item)
				var item openAIItem
				if err := json.Unmarshal(event.Item, &item); err != nil {
					return false, err
				}
				if item.Type == "function_call" && item.Arguments != "" {
					arguments[*event.OutputIndex] = item.Arguments
				}
			case "response.output_text.delta":
				if event.Delta != "" {
					return false, emit(event.Delta)
				}
			case "response.function_call_arguments.delta", "response.function_call_arguments.done":
				if event.OutputIndex == nil {
					return false, errors.New("OpenAI tool arguments are missing an output index")
				}
				if event.Type == "response.function_call_arguments.delta" {
					arguments[*event.OutputIndex] += event.Delta
				} else {
					arguments[*event.OutputIndex] = event.Arguments
				}
				if len(arguments[*event.OutputIndex]) > MaxJSONLLineBytes {
					return false, errors.New("OpenAI tool arguments exceed the byte limit")
				}
			case "response.completed":
				terminal = true
				if event.Response.Output != nil {
					output = *event.Response.Output
				}
				return true, nil
			case "response.failed", "response.incomplete", "error":
				message := event.Response.Error.Message
				if message == "" {
					message = event.Message
				}
				return false, fmt.Errorf("OpenAI %s: %s", event.Type, message)
			}
			return false, nil
		})
	if ctx.Err() != nil {
		return ProviderTurn{}, ctx.Err()
	}
	if err != nil {
		return ProviderTurn{}, err
	}
	if !terminal {
		return ProviderTurn{}, errors.New("OpenAI stream ended before response.completed")
	}
	indexes := make([]int, 0, len(items))
	if output == nil {
		for index := range items {
			indexes = append(indexes, index)
		}
		sort.Ints(indexes)
		output = make([]json.RawMessage, 0, len(indexes))
		for _, index := range indexes {
			output = append(output, items[index])
		}
	}
	var calls []ToolCall
	var text strings.Builder
	for index, raw := range output {
		var item openAIItem
		if err := json.Unmarshal(raw, &item); err != nil {
			return ProviderTurn{}, fmt.Errorf("invalid OpenAI output item: %w", err)
		}
		if item.Type == "" {
			return ProviderTurn{}, errors.New("OpenAI output item is missing its type")
		}
		switch item.Type {
		case "function_call":
			if item.Arguments == "" {
				outputIndex := index
				if len(indexes) > 0 {
					outputIndex = indexes[index]
				}
				item.Arguments = arguments[outputIndex]
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(raw, &fields); err != nil {
					return ProviderTurn{}, err
				}
				fields["arguments"], _ = json.Marshal(item.Arguments)
				output[index], err = json.Marshal(fields)
				if err != nil {
					return ProviderTurn{}, err
				}
			}
			args := bytes.TrimSpace([]byte(item.Arguments))
			if item.Name == "" || item.CallID == "" || len(args) == 0 || args[0] != '{' || !utf8.Valid(args) || !json.Valid(args) {
				return ProviderTurn{}, errors.New("invalid function call or arguments in OpenAI response")
			}
			calls = append(calls, ToolCall{CallID: item.CallID, Name: item.Name, Arguments: args})
		case "message":
			for _, part := range item.Content {
				if part.Type == "output_text" {
					text.WriteString(part.Text)
				}
			}
		}
	}
	return ProviderTurn{Text: text.String(), Output: output, ToolCalls: calls, Assistant: Message{
		Role: "assistant", Text: text.String(), ToolCalls: calls, Replay: cloneRaw(output),
	}}, nil
}

func openAIInput(history []Message) ([]json.RawMessage, error) {
	input := make([]json.RawMessage, 0, len(history))
	for _, message := range history {
		if message.Role == "assistant" && message.Replay != nil {
			input = append(input, cloneRaw(message.Replay)...)
			continue
		}
		switch message.Role {
		case "user", "assistant":
			if message.Text != "" || message.Role == "user" {
				raw, err := json.Marshal(struct {
					Role    string `json:"role"`
					Content string `json:"content"`
				}{message.Role, message.Text})
				if err != nil {
					return nil, err
				}
				input = append(input, raw)
			}
			for _, call := range message.ToolCalls {
				raw, err := json.Marshal(struct {
					Type      string `json:"type"`
					CallID    string `json:"call_id"`
					Name      string `json:"name"`
					Arguments string `json:"arguments"`
				}{"function_call", call.CallID, call.Name, string(call.Arguments)})
				if err != nil {
					return nil, err
				}
				input = append(input, raw)
			}
		case "tool_result":
			if message.Result == nil {
				return nil, errors.New("tool_result history is missing its result")
			}
			raw, err := json.Marshal(struct {
				Type   string `json:"type"`
				CallID string `json:"call_id"`
				Output string `json:"output"`
			}{"function_call_output", message.Result.CallID, message.Result.Output})
			if err != nil {
				return nil, err
			}
			input = append(input, raw)
		default:
			return nil, fmt.Errorf("unknown history role: %s", message.Role)
		}
	}
	return input, nil
}
