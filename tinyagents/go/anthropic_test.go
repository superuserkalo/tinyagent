package tinyagent_test

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"tinyagent"
)

func TestAnthropicSSEToolLoopAndReplay(t *testing.T) {
	scripts := []string{fixture(t, "anthropic_tool"), fixture(t, "anthropic_text")}
	var bodies []*fixtureBody
	requests := 0
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		var payload struct {
			Stream   bool
			Messages []struct {
				Role    string
				Content []json.RawMessage
			}
		}
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			return nil, err
		}
		if request.Header.Get("X-Api-Key") != "fixture" || request.Header.Get("Anthropic-Version") != "2023-06-01" || !payload.Stream {
			t.Error("provider request omitted required headers or streaming")
		}
		if requests == 1 {
			if len(payload.Messages) != 3 ||
				!strings.Contains(string(payload.Messages[1].Content[0]), `"signature":"signed-fixture"`) ||
				!strings.Contains(string(payload.Messages[1].Content[1]), `"input":{"text":"café"}`) ||
				!strings.Contains(string(payload.Messages[2].Content[0]), `"content":"café"`) {
				t.Errorf("lost thinking signature, buffered arguments, or result: %#v", payload.Messages)
			}
		}
		body := &fixtureBody{Reader: io.MultiReader(strings.NewReader(scripts[requests]), failReader{})}
		requests++
		bodies = append(bodies, body)
		return &http.Response{StatusCode: 200, Body: body}, nil
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
	result, err := newAgent(t, &tinyagent.AnthropicProvider{APIKey: "fixture", Client: client}, tool).Run(
		context.Background(), "go", tinyagent.RunOptions{})
	if err != nil || result.Text != "done\ncafé" {
		t.Fatalf("result=%#v error=%v", result, err)
	}
	metadata := string(result.History[1].Replay[0])
	for _, value := range []string{`"id":"message_tool"`, `"input_tokens":8`, `"output_tokens":12`, `"fixture_metadata":"keep"`} {
		if !strings.Contains(metadata, value) {
			t.Errorf("missing replay metadata %s in %s", value, metadata)
		}
	}
	for _, body := range bodies {
		if !body.closed {
			t.Error("provider response body was not closed")
		}
	}
}

func TestAnthropicRejectsMalformedOrIncompleteStreams(t *testing.T) {
	start := "data: {\"type\":\"message_start\",\"message\":{\"id\":\"m\",\"content\":[]}}\n\n"
	tool := "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"c\",\"name\":\"echo\",\"input\":[]}}\n\n"
	stop := "data: {\"type\":\"message_stop\"}\n\n"
	for name, stream := range map[string]string{
		"missing start":     stop,
		"missing stop":      start,
		"unfinished block":  start + tool + stop,
		"invalid arguments": start + tool + "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n" + stop,
		"unknown block":     start + "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n" + stop,
		"provider error":    start + "data: {\"type\":\"error\",\"error\":{\"message\":\"failed\"}}\n\n",
		"max tokens":        start + "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"max_tokens\"}}\n\n" + stop,
	} {
		t.Run(name, func(t *testing.T) {
			body := &fixtureBody{Reader: strings.NewReader(stream)}
			provider := &tinyagent.AnthropicProvider{APIKey: "fixture", Client: &http.Client{Transport: roundTripFunc(
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
