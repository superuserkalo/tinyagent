// Package tinyagent runs a native, in-memory model and tool loop.
package tinyagent

import (
	"context"
	"encoding/json"
)

const (
	ProtocolVersion      = 1
	MaxJSONLLineBytes    = 1_048_576
	MaxHTTPBodyBytes     = 1_048_576
	MaxQueuedOutputBytes = 4_194_304
	MaxToolOutputBytes   = 131_072
)

// Message preserves neutral content and the provider's opaque replay data.
type Message struct {
	Role      string            `json:"role"`
	Text      string            `json:"text,omitempty"`
	ToolCalls []ToolCall        `json:"toolCalls,omitempty"`
	Result    *ToolResult       `json:"result,omitempty"`
	Replay    []json.RawMessage `json:"replay,omitempty"`
}

type ToolCall struct {
	CallID    string          `json:"callId"`
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

type ToolResult struct {
	CallID  string `json:"callId"`
	Name    string `json:"name"`
	Output  string `json:"output"`
	IsError bool   `json:"isError"`
}

type ProviderRequest struct {
	Model        string
	Instructions string
	History      []Message
	Tools        []ToolDefinition
}

type ProviderTurn struct {
	Text      string
	Assistant Message
	Output    []json.RawMessage
	ToolCalls []ToolCall
}

// Provider buffers complete tool arguments and calls emit only for text deltas.
// It must stop on context cancellation or a non-nil error returned by emit.
// Callbacks are synchronous and must finish before Stream returns.
type Provider interface {
	Stream(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error)
}

type RunResult struct {
	Text    string            `json:"text"`
	History []Message         `json:"history"`
	Output  []json.RawMessage `json:"output"`
}

type RunError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

type Event struct {
	Type       string          `json:"type"`
	RunID      string          `json:"runId"`
	Sequence   uint64          `json:"sequence"`
	Delta      string          `json:"delta,omitempty"`
	ToolCallID string          `json:"toolCallId,omitempty"`
	ToolName   string          `json:"toolName,omitempty"`
	Arguments  json.RawMessage `json:"arguments,omitempty"`
	Output     *string         `json:"output,omitempty"`
	IsError    *bool           `json:"isError,omitempty"`
	Status     string          `json:"status,omitempty"`
	Text       *string         `json:"text,omitempty"`
	Error      *RunError       `json:"error,omitempty"`
}

// RunOptions belongs to one run. Agent can serve independent sessions concurrently.
type RunOptions struct {
	RunID   string
	History []Message
	OnEvent func(Event) error
}
