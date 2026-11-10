package tinyagent_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"

	"tinyagent"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

type fixtureBody struct {
	io.Reader
	closed bool
}

func (body *fixtureBody) Close() error {
	body.closed = true
	return nil
}

func fixture(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile("conformance/providers/" + name + ".sse")
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestOpenAISSEToolLoopAndReplay(t *testing.T) {
	scripts := []string{fixture(t, "openai_tool"), fixture(t, "openai_text")}
	var bodies []*fixtureBody
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		var payload struct {
			Stream  bool
			Input   []json.RawMessage
			Tools   []json.RawMessage
			Include []string
		}
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			return nil, err
		}
		if request.Header.Get("Authorization") != "Bearer fixture" || !payload.Stream || len(payload.Tools) != 1 {
			t.Error("provider request omitted headers, stream, or tools")
		}
		if len(payload.Include) != 1 || payload.Include[0] != "reasoning.encrypted_content" {
			t.Errorf("provider did not request encrypted reasoning: %v", payload.Include)
		}
		if requests == 1 {
			if len(payload.Input) != 4 || !strings.Contains(string(payload.Input[1]), "opaque-fixture") ||
				!strings.Contains(string(payload.Input[2]), `\"text\":\"café\"`) ||
				!strings.Contains(string(payload.Input[3]), `"output":"café"`) {
				t.Errorf("replay lost provider metadata, buffered arguments, or observation: %s", payload.Input)
			}
		}
		body := &fixtureBody{Reader: strings.NewReader(scripts[requests])}
		requests++
		bodies = append(bodies, body)
		return &http.Response{StatusCode: 200, Body: body, Header: make(http.Header)}, nil
	})}
	tool, err := tinyagent.NewTool(tinyagent.ToolDefinition{Name: "echo", Parameters: json.RawMessage(
		`{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}`,
	)}, func(ctx context.Context, raw json.RawMessage) (string, error) {
		var args struct{ Text string }
		err := json.Unmarshal(raw, &args)
		return args.Text, err
	})
	if err != nil {
		t.Fatal(err)
	}
	var deltas strings.Builder
	result, err := newAgent(t, &tinyagent.OpenAIProvider{APIKey: "fixture", Client: client}, tool).Run(
		context.Background(), "go", tinyagent.RunOptions{OnEvent: func(event tinyagent.Event) error {
			deltas.WriteString(event.Delta)
			return nil
		}})
	if err != nil || result.Text != "done\ncafé" || deltas.String() != "Working done\ncafé" {
		t.Fatalf("result=%#v error=%v deltas=%q", result, err, deltas.String())
	}
	for _, body := range bodies {
		if !body.closed {
			t.Error("provider response body was not closed")
		}
	}
}

func TestOpenAIRejectsMalformedOrIncompleteStreams(t *testing.T) {
	for name, stream := range map[string]string{
		"incomplete":          "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n",
		"invalid JSON":        "data: {\n\n",
		"null event":          "data: null\n\n",
		"null output":         "data: {\"type\":\"response.completed\",\"response\":{\"output\":[null]}}\n\n",
		"failed":              "data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"message\":\"broken\"}}}\n\n",
		"malformed arguments": "data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"function_call\",\"name\":\"echo\",\"call_id\":\"c\",\"arguments\":\"[]\"}]}}\n\n",
	} {
		t.Run(name, func(t *testing.T) {
			body := &fixtureBody{Reader: strings.NewReader(stream)}
			provider := &tinyagent.OpenAIProvider{APIKey: "fixture", Client: &http.Client{Transport: roundTripFunc(
				func(*http.Request) (*http.Response, error) { return &http.Response{StatusCode: 200, Body: body}, nil },
			)}}
			if _, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{}); err == nil {
				t.Fatal("accepted invalid provider stream")
			}
			if !body.closed {
				t.Fatal("response body remained open")
			}
		})
	}
}

type failReader struct{}

func (failReader) Read([]byte) (int, error) { return 0, errors.New("read after terminal event") }

func TestOpenAITerminalEventClosesWithoutWaitingForEOF(t *testing.T) {
	body := &fixtureBody{Reader: io.MultiReader(strings.NewReader(fixture(t, "openai_text")), failReader{})}
	provider := &tinyagent.OpenAIProvider{APIKey: "fixture", Client: &http.Client{Transport: roundTripFunc(
		func(*http.Request) (*http.Response, error) { return &http.Response{StatusCode: 200, Body: body}, nil },
	)}}
	if _, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{}); err != nil {
		t.Fatal(err)
	}
	if !body.closed {
		t.Fatal("terminal event left response body open")
	}
}

func TestOpenAINeutralHistoryWithoutReplay(t *testing.T) {
	provider := &tinyagent.OpenAIProvider{APIKey: "fixture", Client: &http.Client{Transport: roundTripFunc(
		func(request *http.Request) (*http.Response, error) {
			body, err := io.ReadAll(request.Body)
			if err != nil {
				return nil, err
			}
			if !strings.Contains(string(body), `"content":"keep"`) {
				t.Error("neutral assistant history was dropped")
			}
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(fixture(t, "openai_text")))}, nil
		},
	)}}
	_, err := newAgent(t, provider).Run(context.Background(), "go", tinyagent.RunOptions{
		History: []tinyagent.Message{{Role: "assistant", Text: "keep"}},
	})
	if err != nil {
		t.Fatal(err)
	}
}
