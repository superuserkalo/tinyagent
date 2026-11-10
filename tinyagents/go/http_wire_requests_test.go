package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

type httpByteReader struct{ io.Reader }

func (reader httpByteReader) Read(data []byte) (int, error) {
	return reader.Reader.Read(data[:min(1, len(data))])
}

func readWireEvents(t *testing.T, response *http.Response, status string) []Event {
	t.Helper()
	defer response.Body.Close()
	if response.Header.Get("Content-Type") != "application/jsonl; charset=utf-8" || !response.Close || len(response.TransferEncoding) != 0 {
		t.Fatalf("unexpected prompt framing: headers=%v close=%v encodings=%v", response.Header, response.Close, response.TransferEncoding)
	}
	data, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	lines := bytes.Split(bytes.TrimSpace(data), []byte{'\n'})
	var events []Event
	for index, line := range lines {
		var event Event
		if len(line) > MaxJSONLLineBytes || json.Unmarshal(line, &event) != nil || event.Sequence != uint64(index+1) {
			t.Fatalf("invalid event: %s", line)
		}
		if index > 0 && (event.RunID != events[0].RunID || events[index-1].Type == "agent_end") {
			t.Fatalf("invalid event order: %s", data)
		}
		events = append(events, event)
	}
	if len(events) < 2 || events[0].Type != "agent_start" || events[len(events)-1].Status != status {
		t.Fatalf("invalid run events: %s", data)
	}
	return events
}

func TestHTTPWireRequestBodies(t *testing.T) {
	var calls atomic.Int32
	server, client := startPipeHTTP(t, httpProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		calls.Add(1)
		return httpText("ok"), nil
	}), nil)
	path, _ := pipeHTTPSession(t, server, client)
	for _, scenario := range []struct {
		name, body string
		types      []string
		chunked    bool
		status     int
	}{
		{"fixed", `{"message":"hello 🌍"}`, []string{"application/json; charset=utf-8"}, false, 200},
		{"fragmented chunks", `{"message":"hello 🌍"}`, []string{"application/json"}, true, 200},
		{"nonobject", `[]`, []string{"application/json"}, false, 400},
		{"invalid JSON", `{"message":`, []string{"application/json"}, false, 400},
		{"invalid UTF-8", "\xff", []string{"application/json"}, true, 400},
		{"wrong type", `{}`, []string{"text/plain"}, false, 415},
		{"wrong charset", `{}`, []string{"application/json; charset=latin1"}, false, 415},
		{"duplicate type", `{"message":"go"}`, []string{"application/json", "text/plain"}, false, 415},
		{"exact byte limit", `{"message":"` + strings.Repeat("x", MaxHTTPBodyBytes-len(`{"message":""}`)) + `"}`, []string{"application/json"}, false, 200},
		{"declared byte overflow", strings.Repeat("x", MaxHTTPBodyBytes+1), []string{"application/json"}, false, 413},
		{"chunked byte overflow", strings.Repeat("x", MaxHTTPBodyBytes+1), []string{"application/json"}, true, 413},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			var body io.Reader = strings.NewReader(scenario.body)
			if scenario.chunked {
				if len(scenario.body) < 100 {
					body = httpByteReader{Reader: body}
				} else {
					body = io.NopCloser(body)
				}
			}
			request, _ := http.NewRequest("POST", path+"/prompts", body)
			request.Header.Set("Authorization", "Bearer secret")
			request.Header["Content-Type"] = scenario.types
			before := calls.Load()
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			if response.StatusCode == 200 {
				readWireEvents(t, response, "completed")
			} else {
				io.Copy(io.Discard, response.Body)
				response.Body.Close()
			}
			if response.StatusCode != scenario.status {
				t.Errorf("status=%d want=%d", response.StatusCode, scenario.status)
			}
			wantCalls := before
			if scenario.status == 200 {
				wantCalls++
			}
			if calls.Load() != wantCalls {
				t.Fatalf("invalid body reached provider: calls=%d want=%d", calls.Load(), wantCalls)
			}
		})
	}
}

func TestHTTPWireRoutesAndIndependentSessions(t *testing.T) {
	entered := make(chan struct{})
	server, client := startPipeHTTP(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		if request.History[len(request.History)-1].Text == "wait" {
			close(entered)
			<-ctx.Done()
			return ProviderTurn{}, ctx.Err()
		}
		return httpText("done"), nil
	}), nil)
	health := requestHTTP(t, client, "GET", server.URL()+"/health", nil, 200)
	data, err := io.ReadAll(health.Body)
	health.Body.Close()
	if err != nil || string(data) != "{\"status\":\"ok\",\"protocolVersion\":1}\n" || health.Header.Get("Access-Control-Allow-Origin") != "" {
		t.Fatalf("health=%s error=%v headers=%v", data, err, health.Header)
	}
	path, current := pipeHTTPSession(t, server, client)
	response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"wait"}`), 200)
	defer response.Body.Close()
	decoder := json.NewDecoder(response.Body)
	start := readHTTPEvent(t, decoder)
	awaitHTTP(t, entered)
	busy := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"second"}`), 409)
	io.Copy(io.Discard, busy.Body)
	busy.Body.Close()
	other, _ := pipeHTTPSession(t, server, client)
	readWireEvents(t, requestHTTP(t, client, "POST", other+"/prompts", strings.NewReader(`{"message":"independent"}`), 200), "completed")
	for _, want := range []bool{true, false} {
		canceled := requestHTTP(t, client, "POST", path+"/cancel", nil, 200)
		var accepted struct {
			Accepted bool    `json:"accepted"`
			RunID    *string `json:"runId"`
		}
		err := json.NewDecoder(canceled.Body).Decode(&accepted)
		canceled.Body.Close()
		if err != nil || accepted.Accepted != want || (want && (accepted.RunID == nil || *accepted.RunID != start.RunID)) || (!want && accepted.RunID != nil) {
			t.Fatalf("cancel=%#v want=%v error=%v", accepted, want, err)
		}
		if want {
			if end := readHTTPEvent(t, decoder); end.Status != "aborted" || end.RunID != start.RunID {
				t.Fatalf("cancelled event=%#v", end)
			}
			if _, err := io.Copy(io.Discard, response.Body); err != nil {
				t.Fatal(err)
			}
		}
	}
	if current.state().MessageCount != 0 {
		t.Fatal("cancelled request changed history")
	}
	deleted := requestHTTP(t, client, "DELETE", path, nil, 204)
	deleted.Body.Close()
	for _, address := range []string{path + "/prompts", server.URL() + "/unknown"} {
		missing := requestHTTP(t, client, "POST", address, strings.NewReader(`{"message":"go"}`), 404)
		io.Copy(io.Discard, missing.Body)
		missing.Body.Close()
	}
}

func TestHTTPWireOutputLimits(t *testing.T) {
	for _, stage := range []string{"delta", "terminal", "tool"} {
		t.Run(stage, func(t *testing.T) {
			server, client := startPipeHTTP(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
				large := strings.Repeat("x", MaxJSONLLineBytes)
				switch stage {
				case "delta":
					return ProviderTurn{}, emit(large)
				case "tool":
					calls := []ToolCall{{CallID: "call", Name: "missing", Arguments: json.RawMessage(`{"text":"` + large + `"}`)}}
					return ProviderTurn{Assistant: Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
				default:
					return httpText(large), nil
				}
			}), nil)
			path, current := pipeHTTPSession(t, server, client)
			response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"go"}`), 200)
			events := readWireEvents(t, response, "failed")
			last := events[len(events)-1]
			if last.Error == nil || last.Error.Code != "output_too_large" || current.state().MessageCount != 0 {
				t.Fatalf("output limit failed: event=%#v history=%#v", last, current.state())
			}
		})
	}
}
