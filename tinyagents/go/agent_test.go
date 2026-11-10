package tinyagent_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"

	"tinyagent"
)

type providerFunc func(context.Context, tinyagent.ProviderRequest, func(string) error) (tinyagent.ProviderTurn, error)

func (provider providerFunc) Stream(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
	return provider(ctx, request, emit)
}

func completed(text string) tinyagent.ProviderTurn {
	return tinyagent.ProviderTurn{Text: text, Assistant: tinyagent.Message{Role: "assistant", Text: text}}
}

func newAgent(t *testing.T, provider tinyagent.Provider, tools ...*tinyagent.Tool) *tinyagent.Agent {
	t.Helper()
	agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{Model: "fixture", Provider: provider, Tools: tools})
	if err != nil {
		t.Fatal(err)
	}
	return agent
}

func TestSharedToolArguments(t *testing.T) {
	raw, err := os.ReadFile("conformance/tool_arguments.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Cases []struct {
			Name    string
			Schema  json.RawMessage
			Valid   []json.RawMessage
			Invalid []json.RawMessage
		}
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	for _, scenario := range fixture.Cases {
		t.Run(scenario.Name, func(t *testing.T) {
			calls := 0
			tool, err := tinyagent.NewTool(tinyagent.ToolDefinition{Name: "check", Parameters: scenario.Schema},
				func(ctx context.Context, arguments json.RawMessage) (string, error) {
					calls++
					return "ok", nil
				})
			if err != nil {
				t.Fatal(err)
			}
			for _, valid := range scenario.Valid {
				if output, err := tool.Execute(context.Background(), valid); err != nil || output != "ok" {
					t.Fatalf("valid input %s: output=%q error=%v", valid, output, err)
				}
			}
			before := calls
			for _, invalid := range scenario.Invalid {
				original := string(invalid)
				if _, err := tool.Execute(context.Background(), invalid); err == nil {
					t.Fatalf("accepted invalid input: %s", invalid)
				}
				if string(invalid) != original {
					t.Fatal("validation changed arguments")
				}
			}
			if calls != before {
				t.Fatal("invalid arguments reached handler")
			}
		})
	}
}

func TestInvalidToolSchemas(t *testing.T) {
	for _, schema := range []string{`{"type":"nonsense"}`, `{"$ref":"https://example.invalid/schema"}`, `{"$async":true}`} {
		if _, err := tinyagent.NewTool(tinyagent.ToolDefinition{Name: "check", Parameters: json.RawMessage(schema)},
			func(context.Context, json.RawMessage) (string, error) { return "unused", nil }); err == nil {
			t.Fatalf("accepted invalid schema: %s", schema)
		}
	}
}

func TestSequentialToolsRecoverAndBoundObservations(t *testing.T) {
	var handled []string
	tool, err := tinyagent.NewTool(tinyagent.ToolDefinition{Name: "echo", Parameters: json.RawMessage(
		`{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}`,
	)}, func(ctx context.Context, raw json.RawMessage) (string, error) {
		var arguments struct{ Text string }
		if err := json.Unmarshal(raw, &arguments); err != nil {
			return "", err
		}
		handled = append(handled, arguments.Text)
		return strings.Repeat("x", tinyagent.MaxToolOutputBytes+1), nil
	})
	if err != nil {
		t.Fatal(err)
	}
	requests := 0
	provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		requests++
		if requests == 1 {
			if err := emit("working"); err != nil {
				return tinyagent.ProviderTurn{}, err
			}
			calls := []tinyagent.ToolCall{
				{CallID: "bad", Name: "echo", Arguments: json.RawMessage(`{"text":7}`)},
				{CallID: "large", Name: "echo", Arguments: json.RawMessage(`{"text":"hello"}`)},
			}
			return tinyagent.ProviderTurn{Assistant: tinyagent.Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
		}
		for _, message := range request.History[2:] {
			if message.Result == nil || !message.Result.IsError || len(message.Result.Output) > tinyagent.MaxToolOutputBytes {
				t.Fatalf("tool failure not bounded and recoverable: %#v", message)
			}
		}
		if err := emit("done"); err != nil {
			return tinyagent.ProviderTurn{}, err
		}
		return completed("done"), nil
	})
	var kinds []string
	result, err := newAgent(t, provider, tool).Run(context.Background(), "go", tinyagent.RunOptions{
		RunID: "fixture", OnEvent: func(event tinyagent.Event) error {
			kinds = append(kinds, event.Type)
			if event.Sequence != uint64(len(kinds)) || event.RunID != "fixture" {
				t.Fatalf("invalid event identity: %#v", event)
			}
			return nil
		},
	})
	if err != nil || result.Text != "done" || !reflect.DeepEqual(handled, []string{"hello"}) {
		t.Fatalf("result=%#v error=%v handled=%v", result, err, handled)
	}
	want := []string{"agent_start", "text_delta", "tool_execution_start", "tool_execution_end", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"}
	if !reflect.DeepEqual(kinds, want) {
		t.Fatalf("events: %v", kinds)
	}
}

func TestCancellationWinsAfterProviderReturns(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	provider := providerFunc(func(context.Context, tinyagent.ProviderRequest, func(string) error) (tinyagent.ProviderTurn, error) {
		cancel()
		return completed("late"), nil
	})
	var last tinyagent.Event
	result, err := newAgent(t, provider).Run(ctx, "go", tinyagent.RunOptions{OnEvent: func(event tinyagent.Event) error {
		last = event
		return nil
	}})
	if !errors.Is(err, context.Canceled) || result.History != nil || last.Status != "aborted" {
		t.Fatalf("result=%#v error=%v event=%#v", result, err, last)
	}
}

func TestConsumerFailureCancelsProvider(t *testing.T) {
	closed := false
	provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		defer func() { closed = ctx.Err() != nil }()
		_ = emit("one")
		return completed("done"), nil
	})
	failure := errors.New("writer closed")
	_, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{OnEvent: func(event tinyagent.Event) error {
		if event.Type == "text_delta" {
			return failure
		}
		return nil
	}})
	if !errors.Is(err, failure) || !closed {
		t.Fatalf("error=%v closed=%t", err, closed)
	}
}

func TestFailedProviderCannotMutatePriorHistory(t *testing.T) {
	history := []tinyagent.Message{{Role: "assistant", Text: "keep", Replay: []json.RawMessage{json.RawMessage(`{"keep":true}`)}}}
	provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		request.History[0].Text = "changed"
		request.History[0].Replay[0][0] = 'x'
		return tinyagent.ProviderTurn{}, errors.New("failed")
	})
	_, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{History: history})
	if err == nil || history[0].Text != "keep" || string(history[0].Replay[0]) != `{"keep":true}` {
		t.Fatalf("error=%v history=%#v", err, history)
	}
}
