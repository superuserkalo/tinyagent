package tinyagent

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
)

type AgentConfig struct {
	Model        string
	Instructions string
	Provider     Provider
	Tools        []*Tool
}

type Agent struct {
	model        string
	instructions string
	provider     Provider
	tools        map[string]*Tool
	definitions  []ToolDefinition
}

func NewAgent(config AgentConfig) (*Agent, error) {
	if config.Model == "" || config.Provider == nil {
		return nil, errors.New("model and provider are required")
	}
	agent := &Agent{
		model: config.Model, instructions: config.Instructions, provider: config.Provider,
		tools: make(map[string]*Tool), definitions: make([]ToolDefinition, 0, len(config.Tools)),
	}
	for _, tool := range config.Tools {
		if tool == nil || tool.schema == nil {
			return nil, errors.New("tools must be constructed with NewTool")
		}
		definition := tool.Definition()
		if _, exists := agent.tools[definition.Name]; exists {
			return nil, fmt.Errorf("duplicate tool: %s", definition.Name)
		}
		agent.tools[definition.Name] = tool
		agent.definitions = append(agent.definitions, definition)
	}
	return agent, nil
}

// Run emits ordered events through OnEvent and returns history only on success.
// Returning an error from OnEvent cancels the run and closes its provider.
func (a *Agent) Run(parent context.Context, prompt string, options RunOptions) (result RunResult, runErr error) {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	runID := options.RunID
	if runID == "" {
		runID = randomID("r_")
	}
	var sequence uint64
	var consumerErr error
	terminal := false
	emit := func(event Event) error {
		if consumerErr != nil {
			return consumerErr
		}
		sequence++
		event.RunID, event.Sequence = runID, sequence
		terminal = event.Type == "agent_end"
		// Callbacks own their event payload; edits must not change the transcript.
		event.Arguments = bytes.Clone(event.Arguments)
		if event.Text != nil {
			text := *event.Text
			event.Text = &text
		}
		if event.Output != nil {
			output := *event.Output
			event.Output = &output
		}
		if event.IsError != nil {
			isError := *event.IsError
			event.IsError = &isError
		}
		if options.OnEvent != nil {
			consumerErr = options.OnEvent(event)
			if consumerErr != nil {
				cancel()
			}
		}
		return consumerErr
	}
	defer func() {
		if runErr == nil || consumerErr != nil || terminal {
			return
		}
		event := Event{Type: "agent_end", Status: "failed", Error: &RunError{
			Code: "provider_error", Message: boundedObservation(runErr.Error()), Retryable: true,
		}}
		if ctx.Err() != nil {
			event.Status, event.Error = "aborted", nil
			runErr = ctx.Err()
		}
		if err := emit(event); err != nil {
			runErr = err
		}
	}()
	history := cloneHistory(options.History)
	history = append(history, Message{Role: "user", Text: prompt})
	if err := emit(Event{Type: "agent_start"}); err != nil {
		return RunResult{}, err
	}
	for {
		if err := ctx.Err(); err != nil {
			return RunResult{}, err
		}
		definitions := make([]ToolDefinition, len(a.definitions))
		for index, definition := range a.definitions {
			definitions[index] = definition
			definitions[index].Parameters = bytes.Clone(definition.Parameters)
		}
		turn, err := func() (ProviderTurn, error) {
			requestContext, finishRequest := context.WithCancel(ctx)
			defer finishRequest()
			return a.provider.Stream(requestContext, ProviderRequest{
				Model: a.model, Instructions: a.instructions, History: cloneHistory(history), Tools: definitions,
			}, func(delta string) error {
				if err := requestContext.Err(); err != nil {
					return err
				}
				return emit(Event{Type: "text_delta", Delta: delta})
			})
		}()
		if consumerErr != nil {
			return RunResult{}, consumerErr
		}
		if ctx.Err() != nil {
			return RunResult{}, ctx.Err()
		}
		if err != nil {
			return RunResult{}, err
		}
		history = append(history, cloneMessage(turn.Assistant))
		if len(turn.ToolCalls) == 0 {
			output := cloneRaw(turn.Output)
			if err := emit(Event{Type: "agent_end", Status: "completed", Text: &turn.Text}); err != nil {
				return RunResult{}, err
			}
			if err := ctx.Err(); err != nil {
				return RunResult{}, err
			}
			return RunResult{Text: turn.Text, History: cloneHistory(history), Output: output}, nil
		}
		for _, call := range cloneToolCalls(turn.ToolCalls) {
			if err := ctx.Err(); err != nil {
				return RunResult{}, err
			}
			if err := emit(Event{Type: "tool_execution_start", ToolCallID: call.CallID,
				ToolName: call.Name, Arguments: call.Arguments}); err != nil {
				return RunResult{}, err
			}
			var output string
			var toolErr error
			if tool, exists := a.tools[call.Name]; exists {
				output, toolErr = tool.Execute(ctx, call.Arguments)
			} else {
				toolErr = fmt.Errorf("unknown tool: %s", call.Name)
			}
			if ctx.Err() != nil {
				toolErr = ctx.Err()
			}
			isError := toolErr != nil || len(output) > MaxToolOutputBytes
			if toolErr != nil {
				output = toolErr.Error()
			}
			output = boundedObservation(output)
			if err := emit(Event{Type: "tool_execution_end", ToolCallID: call.CallID,
				ToolName: call.Name, Output: &output, IsError: &isError}); err != nil {
				return RunResult{}, err
			}
			if ctx.Err() != nil {
				return RunResult{}, ctx.Err()
			}
			history = append(history, Message{Role: "tool_result", Result: &ToolResult{
				CallID: call.CallID, Name: call.Name, Output: output, IsError: isError,
			}})
		}
	}
}

func boundedObservation(output string) string {
	if len(output) > MaxToolOutputBytes {
		return "Tool output exceeds the 131072-byte limit"
	}
	return output
}

func randomID(prefix string) string {
	var random [16]byte
	if _, err := rand.Read(random[:]); err != nil {
		panic(err) // No safe session identifier can be issued without OS randomness.
	}
	return prefix + hex.EncodeToString(random[:])
}

func cloneRaw(items []json.RawMessage) []json.RawMessage {
	if items == nil {
		return nil
	}
	copy := make([]json.RawMessage, len(items))
	for index, item := range items {
		copy[index] = bytes.Clone(item)
	}
	return copy
}

func cloneHistory(history []Message) []Message {
	copy := make([]Message, len(history))
	for index, message := range history {
		copy[index] = cloneMessage(message)
	}
	return copy
}

func cloneMessage(message Message) Message {
	message.Replay = cloneRaw(message.Replay)
	message.ToolCalls = cloneToolCalls(message.ToolCalls)
	if message.Result != nil {
		result := *message.Result
		message.Result = &result
	}
	return message
}

func cloneToolCalls(calls []ToolCall) []ToolCall {
	copy := append([]ToolCall(nil), calls...)
	for index, call := range calls {
		copy[index].Arguments = bytes.Clone(call.Arguments)
	}
	return copy
}
