package tinyagent_test

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"tinyagent"
)

func TestCancellationDuringFinalDeliveryReturnsNoResult(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	provider := providerFunc(func(context.Context, tinyagent.ProviderRequest, func(string) error) (tinyagent.ProviderTurn, error) {
		return completed("answer"), nil
	})
	var events []tinyagent.Event
	result, err := newAgent(t, provider).Run(ctx, "go", tinyagent.RunOptions{OnEvent: func(event tinyagent.Event) error {
		events = append(events, event)
		if event.Type == "agent_end" {
			cancel()
		}
		return nil
	}})
	if !errors.Is(err, context.Canceled) || !reflect.DeepEqual(result, tinyagent.RunResult{}) {
		t.Fatalf("cancelled final delivery returned result=%#v error=%v", result, err)
	}
	if len(events) != 2 || events[0].Type != "agent_start" || events[1].Status != "completed" {
		t.Fatalf("terminal delivery was repeated: %#v", events)
	}
}

func TestRunSnapshotsHistoryBeforeStartCallback(t *testing.T) {
	history := []tinyagent.Message{{Role: "assistant", Text: "seed", Replay: []json.RawMessage{json.RawMessage(`{"value":1}`)}}}
	provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		if request.History[0].Text != "seed" || string(request.History[0].Replay[0]) != `{"value":1}` {
			t.Fatalf("start callback changed the run's history: %#v", request.History)
		}
		return completed("answer"), nil
	})
	result, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{
		History: history, OnEvent: func(event tinyagent.Event) error {
			if event.Type == "agent_start" {
				history[0].Text = "changed"
				history[0].Replay[0][0] = 'x'
			}
			return nil
		},
	})
	if err != nil || result.History[0].Text != "seed" || string(result.History[0].Replay[0]) != `{"value":1}` {
		t.Fatalf("result=%#v error=%v", result, err)
	}
}

func TestProviderRequestEndsBeforeToolsAndFinalDelivery(t *testing.T) {
	var retained func(string) error
	var requestContext context.Context
	checkClosed := func() {
		t.Helper()
		if !errors.Is(requestContext.Err(), context.Canceled) {
			t.Error("provider request scope remains open")
		}
		if err := retained("late text"); !errors.Is(err, context.Canceled) {
			t.Errorf("retained provider callback returned %v", err)
		}
	}
	tool, err := tinyagent.NewTool(tinyagent.ToolDefinition{Name: "echo", Parameters: json.RawMessage(`{"type":"object"}`)},
		func(ctx context.Context, arguments json.RawMessage) (string, error) {
			checkClosed()
			if ctx.Err() != nil {
				t.Error("ending a provider request cancelled the tool's run")
			}
			return "ok", nil
		})
	if err != nil {
		t.Fatal(err)
	}
	requests := 0
	provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		requests++
		requestContext, retained = ctx, emit
		if requests == 1 {
			calls := []tinyagent.ToolCall{{CallID: "call", Name: "echo", Arguments: json.RawMessage(`{}`)}}
			return tinyagent.ProviderTurn{Assistant: tinyagent.Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
		}
		return completed("answer"), nil
	})
	var events []tinyagent.Event
	result, err := newAgent(t, provider, tool).Run(context.Background(), "go", tinyagent.RunOptions{
		RunID: "request-scopes", OnEvent: func(event tinyagent.Event) error {
			events = append(events, event)
			if event.Type == "tool_execution_start" || event.Type == "agent_end" {
				checkClosed()
			}
			return nil
		},
	})
	if err != nil || result.Text != "answer" || requests != 2 {
		t.Fatalf("result=%#v error=%v requests=%d", result, err, requests)
	}
	checkClosed()
	want := []string{"agent_start", "tool_execution_start", "tool_execution_end", "agent_end"}
	var kinds []string
	for index, event := range events {
		kinds = append(kinds, event.Type)
		if event.Sequence != uint64(index+1) || event.RunID != "request-scopes" {
			t.Fatalf("invalid event identity: %#v", event)
		}
	}
	if !reflect.DeepEqual(kinds, want) {
		t.Fatalf("provider emitted outside its request: %v", kinds)
	}
}

func TestEventAndHandlerMutationsCannotChangeTranscript(t *testing.T) {
	arguments := json.RawMessage(`{"value":1}`)
	tool, err := tinyagent.NewTool(tinyagent.ToolDefinition{Name: "echo", Parameters: json.RawMessage(`{"type":"object"}`)},
		func(ctx context.Context, raw json.RawMessage) (string, error) {
			if string(raw) != `{"value":1}` {
				t.Fatalf("handler received changed arguments: %s", raw)
			}
			copy(raw, `{"value":9}`)
			return "original output", nil
		})
	if err != nil {
		t.Fatal(err)
	}
	direct := json.RawMessage(`{"value":1}`)
	if output, err := tool.Execute(context.Background(), direct); err != nil || output != "original output" || string(direct) != `{"value":1}` {
		t.Fatalf("handler changed its caller's arguments: %s output=%q error=%v", direct, output, err)
	}
	requests := 0
	provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		requests++
		if requests == 1 {
			calls := []tinyagent.ToolCall{{CallID: "call", Name: "echo", Arguments: arguments}}
			return tinyagent.ProviderTurn{Assistant: tinyagent.Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
		}
		if string(request.History[1].ToolCalls[0].Arguments) != `{"value":1}` {
			t.Errorf("handler changed replayed arguments: %s", request.History[1].ToolCalls[0].Arguments)
		}
		observation := request.History[2].Result
		if observation.Output != "original output" || observation.IsError {
			t.Errorf("event consumer changed the tool observation: %#v", observation)
		}
		return completed("answer"), nil
	})
	result, err := newAgent(t, provider, tool).Run(context.Background(), "go", tinyagent.RunOptions{OnEvent: func(event tinyagent.Event) error {
		switch event.Type {
		case "tool_execution_start":
			copy(event.Arguments, `{"value":3}`)
		case "tool_execution_end":
			*event.Output, *event.IsError = "changed output", true
		case "agent_end":
			*event.Text = "changed answer"
		}
		return nil
	}})
	if err != nil || result.Text != "answer" {
		t.Fatalf("event consumer changed the result: %#v error=%v", result, err)
	}
	if string(arguments) != `{"value":1}` {
		t.Fatalf("handler changed provider-owned arguments: %s", arguments)
	}
}

func TestProviderReturnedValuesAreDetachedBeforeCallbacks(t *testing.T) {
	turn := completed("answer")
	turn.Assistant.Replay = []json.RawMessage{json.RawMessage(`{"value":1}`)}
	turn.Output = []json.RawMessage{json.RawMessage(`{"value":2}`)}
	provider := providerFunc(func(context.Context, tinyagent.ProviderRequest, func(string) error) (tinyagent.ProviderTurn, error) {
		return turn, nil
	})
	result, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{OnEvent: func(event tinyagent.Event) error {
		if event.Type == "agent_end" {
			turn.Assistant.Replay[0][0] = 'x'
			turn.Output[0][0] = 'x'
		}
		return nil
	}})
	if err != nil || string(result.History[1].Replay[0]) != `{"value":1}` || string(result.Output[0]) != `{"value":2}` {
		t.Fatalf("provider aliases changed completed values: %#v error=%v", result, err)
	}
}
