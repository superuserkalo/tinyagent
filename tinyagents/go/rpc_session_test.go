package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
	"time"
)

type rpcProvider func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error)

func (provider rpcProvider) Stream(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
	return provider(ctx, request, emit)
}

type rpcHeldFrame struct {
	bytes.Buffer
	kind    string
	entered chan struct{}
	release chan struct{}
	failure error
}

func (output *rpcHeldFrame) Write(data []byte) (int, error) {
	var event Event
	if err := json.Unmarshal(data, &event); err != nil {
		return 0, err
	}
	if event.Type == output.kind {
		output.kind = "" // Hold exactly one frame, after its prefix is written.
		output.Buffer.Write(data[:3])
		close(output.entered)
		<-output.release
		if output.failure != nil {
			return 3, output.failure
		}
		output.Buffer.Write(data[3:])
		return len(data), nil
	}
	return output.Buffer.Write(data)
}

func awaitRPC(t *testing.T, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("RPC operation did not finish")
	}
}

func rpcCommand(t *testing.T, ctx context.Context, agent *Agent, current **session, writer *rpcWriter, line string) {
	t.Helper()
	var request map[string]json.RawMessage
	if err := json.Unmarshal([]byte(line), &request); err != nil {
		t.Fatal(err)
	}
	if err := handleRPC(ctx, agent, current, writer, request); err != nil {
		t.Fatal(err)
	}
}

func TestRPCPendingAcknowledgementReservesSession(t *testing.T) {
	provider := rpcProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		t.Error("provider started behind a cancelled acknowledgement")
		return ProviderTurn{}, nil
	})
	agent, err := NewAgent(AgentConfig{Model: "fixture", Provider: provider})
	if err != nil {
		t.Fatal(err)
	}
	output := &rpcHeldFrame{kind: "response", entered: make(chan struct{}), release: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	writer := newRPCWriter(output, cancel)
	current := &session{}
	rpcCommand(t, ctx, agent, &current, writer, `{"id":"first","type":"prompt","message":"go"}`)
	awaitRPC(t, output.entered)
	if state := current.state(); state.Status != "running" || state.MessageCount != 0 {
		t.Fatalf("pending acknowledgement did not reserve session: %#v", state)
	}
	rpcCommand(t, ctx, agent, &current, writer, `{"id":"busy","type":"prompt","message":"second"}`)
	rpcCommand(t, ctx, agent, &current, writer, `{"id":"abort","type":"abort"}`)
	close(output.release)
	current.close()
	if err := writer.close(); err != nil {
		t.Fatal(err)
	}
	lines := bytes.Split(bytes.TrimSpace(output.Bytes()), []byte{'\n'})
	if len(lines) != 5 {
		t.Fatalf("unexpected cancellation frames: %s", output.Bytes())
	}
	var ack, busy, aborted rpcReply
	json.Unmarshal(lines[0], &ack)
	json.Unmarshal(lines[1], &busy)
	json.Unmarshal(lines[2], &aborted)
	if ack.ID == nil || *ack.ID != "first" || busy.Error == nil || busy.Error.Code != "busy" || aborted.Command != "abort" {
		t.Fatalf("commands were lost behind partial output: %s", output.Bytes())
	}
	for index, line := range lines[3:] {
		var event Event
		if json.Unmarshal(line, &event) != nil || event.Sequence != uint64(index+1) || (index == 1 && event.Status != "aborted") {
			t.Fatalf("cancelled acknowledgement damaged event order: %s", output.Bytes())
		}
	}
}

func TestRPCHistoryWaitsForTerminalDelivery(t *testing.T) {
	for _, ending := range []string{"complete", "abort", "failure"} {
		t.Run(ending, func(t *testing.T) {
			provider := rpcProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
				return ProviderTurn{Text: "answer", Assistant: Message{Role: "assistant", Text: "answer"}}, nil
			})
			agent, err := NewAgent(AgentConfig{Model: "fixture", Provider: provider})
			if err != nil {
				t.Fatal(err)
			}
			output := &rpcHeldFrame{kind: "agent_end", entered: make(chan struct{}), release: make(chan struct{})}
			if ending == "failure" {
				output.failure = io.ErrClosedPipe
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			writer := newRPCWriter(output, cancel)
			current := &session{history: []Message{{Role: "assistant", Text: "seed"}}}
			rpcCommand(t, ctx, agent, &current, writer, `{"type":"prompt","message":"go"}`)
			awaitRPC(t, output.entered)
			if state := current.state(); state.Status != "running" || state.MessageCount != 1 {
				t.Fatalf("terminal prefix committed history: %#v", state)
			}
			current.mu.Lock()
			run := current.active
			current.mu.Unlock()
			if ending == "abort" {
				rpcCommand(t, ctx, agent, &current, writer, `{"type":"abort"}`)
			}
			close(output.release)
			awaitRPC(t, run.done)
			if err := writer.close(); !errors.Is(err, output.failure) {
				t.Fatalf("transport error=%v want=%v", err, output.failure)
			}
			want := 1
			if ending == "complete" {
				want = 3
			}
			if state := current.state(); state.Status != "idle" || state.MessageCount != want {
				t.Fatalf("terminal delivery history=%#v want=%d", state, want)
			}
			if ending != "failure" {
				if count := strings.Count(output.String(), `"type":"agent_end"`); count != 1 {
					t.Fatalf("terminal event count=%d", count)
				}
				for _, line := range bytes.Split(bytes.TrimSpace(output.Bytes()), []byte{'\n'}) {
					if !json.Valid(line) {
						t.Fatal("partial final output interleaved with a command response")
					}
				}
			}
		})
	}
}

func TestRPCSlowToolCleanupKeepsOutputOpen(t *testing.T) {
	entered, cleaning, release := make(chan struct{}), make(chan struct{}), make(chan struct{})
	tool, err := NewTool(ToolDefinition{Name: "wait", Parameters: json.RawMessage(`{"type":"object"}`)},
		func(ctx context.Context, arguments json.RawMessage) (string, error) {
			close(entered)
			<-ctx.Done()
			close(cleaning)
			<-release
			return "", ctx.Err()
		})
	if err != nil {
		t.Fatal(err)
	}
	provider := rpcProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		calls := []ToolCall{{CallID: "call", Name: "wait", Arguments: json.RawMessage(`{}`)}}
		return ProviderTurn{ToolCalls: calls, Assistant: Message{Role: "assistant", ToolCalls: calls}}, nil
	})
	agent, err := NewAgent(AgentConfig{Model: "fixture", Provider: provider, Tools: []*Tool{tool}})
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	writer := newRPCWriter(&output, cancel)
	current := &session{}
	rpcCommand(t, ctx, agent, &current, writer, `{"type":"prompt","message":"go"}`)
	awaitRPC(t, entered)
	done := make(chan struct{})
	go func() {
		current.close()
		close(done)
	}()
	awaitRPC(t, cleaning)
	select {
	case <-done:
		t.Fatal("session returned before tool cleanup")
	case <-time.After(rpcDrainTimeout + 100*time.Millisecond):
	}
	close(release)
	awaitRPC(t, done)
	if err := writer.close(); err != nil {
		t.Fatalf("healthy output closed during cleanup: %v", err)
	}
	lines := bytes.Split(bytes.TrimSpace(output.Bytes()), []byte{'\n'})
	if len(lines) != 5 {
		t.Fatalf("missing cancellation events: %s", output.Bytes())
	}
	var toolEnd, agentEnd Event
	json.Unmarshal(lines[3], &toolEnd)
	json.Unmarshal(lines[4], &agentEnd)
	if toolEnd.Type != "tool_execution_end" || toolEnd.IsError == nil || !*toolEnd.IsError || agentEnd.Status != "aborted" {
		t.Fatalf("tool cleanup lost its paired events: %s", output.Bytes())
	}
}
