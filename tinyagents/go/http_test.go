package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type httpProvider func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error)

func (provider httpProvider) Stream(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
	return provider(ctx, request, emit)
}

func newHTTPHandler(t *testing.T, provider Provider, tools ...*Tool) *httpHandler {
	t.Helper()
	agent, err := NewAgent(AgentConfig{Model: "fixture", Provider: provider, Tools: tools})
	if err != nil {
		t.Fatal(err)
	}
	handler := &httpHandler{agent: agent, sessions: make(map[string]*session)}
	t.Cleanup(handler.shutdown)
	return handler
}

func httpText(text string) ProviderTurn {
	return ProviderTurn{Text: text, Assistant: Message{Role: "assistant", Text: text}}
}

func recordRequest(handler http.Handler, method, path, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	if body != "" {
		request.Header.Set("Content-Type", "application/json")
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
}

func createHTTPSession(t *testing.T, handler http.Handler) string {
	t.Helper()
	response := recordRequest(handler, "POST", "/sessions", "")
	var body struct{ SessionID string }
	if response.Code != 201 || json.Unmarshal(response.Body.Bytes(), &body) != nil || body.SessionID == "" {
		t.Fatalf("create session: %d %s", response.Code, response.Body.String())
	}
	return "/sessions/" + body.SessionID
}

func decodeHTTPEvents(t *testing.T, response *httptest.ResponseRecorder, status string) []Event {
	t.Helper()
	if response.Code != 200 || response.Header().Get("Content-Type") != "application/jsonl; charset=utf-8" {
		t.Fatalf("prompt response: %d %s", response.Code, response.Body.String())
	}
	decoder := json.NewDecoder(response.Body)
	decoder.DisallowUnknownFields()
	var events []Event
	for {
		var event Event
		if err := decoder.Decode(&event); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			t.Fatal(err)
		}
		if len(events) > 0 && (events[len(events)-1].Type == "agent_end" || events[0].RunID != event.RunID) {
			t.Fatalf("event follows terminal event or changes run ID: %#v", event)
		}
		events = append(events, event)
		if event.Sequence != uint64(len(events)) || event.RunID == "" {
			t.Fatalf("event identity: %#v", event)
		}
	}
	if len(events) < 2 || events[0].Type != "agent_start" || events[len(events)-1].Status != status {
		t.Fatalf("event lifecycle: %#v", events)
	}
	return events
}

func awaitHTTP(t *testing.T, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("HTTP operation did not finish")
	}
}

func TestHTTPExposure(t *testing.T) {
	for _, scenario := range []struct {
		options HTTPOptions
		address string
	}{
		{HTTPOptions{}, "127.0.0.1:8080"},
		{HTTPOptions{Address: "localhost:0"}, "127.0.0.1:0"},
		{HTTPOptions{Address: "[::1]:1234"}, "[::1]:1234"},
		{HTTPOptions{Address: "0.0.0.0:1234", Token: "secret", AllowRemote: true}, "0.0.0.0:1234"},
		{HTTPOptions{Address: "0.0.0.0:1234"}, ""},
		{HTTPOptions{Address: "0.0.0.0:1234", Token: "secret"}, ""},
		{HTTPOptions{Address: "0.0.0.0:1234", AllowRemote: true}, ""},
		{HTTPOptions{Address: "[::]:1234", Token: "secret"}, ""},
		{HTTPOptions{Address: "example.com:1234", AllowRemote: true, Token: "secret"}, ""},
		{HTTPOptions{Address: "127.0.0.1:65536"}, ""},
		{HTTPOptions{Address: "127.0.0.1:abc"}, ""},
		{HTTPOptions{Token: "token\n"}, ""},
		{HTTPOptions{Token: "two words"}, ""},
	} {
		address, err := listenAddress(scenario.options)
		if address != scenario.address || (err != nil) != (scenario.address == "") {
			t.Fatalf("options=%#v address=%q error=%v", scenario.options, address, err)
		}
	}
	if _, err := Serve(nil, HTTPOptions{}); err == nil {
		t.Fatal("accepted a nil agent")
	}
}

func TestHTTPAuthenticationAndRoutes(t *testing.T) {
	handler := newHTTPHandler(t, httpProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		return httpText("done"), nil
	}))
	handler.token = "secret"
	for _, authorization := range []string{"", "Bearer wrong", "bearer secret", "Bearer secret"} {
		request := httptest.NewRequest("GET", "/health", nil)
		request.Header.Set("Authorization", authorization)
		request.Header.Set("Origin", "https://example.invalid")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		want := 401
		if authorization == "Bearer secret" {
			want = 200
		}
		if response.Code != want || response.Header().Get("Access-Control-Allow-Origin") != "" {
			t.Fatalf("authorization=%q status=%d headers=%v", authorization, response.Code, response.Header())
		}
	}
	handler.token = ""
	if response := recordRequest(handler, "GET", "/health", ""); response.Body.String() != "{\"status\":\"ok\",\"protocolVersion\":1}\n" {
		t.Fatal(response.Body.String())
	}
	path := createHTTPSession(t, handler)
	if response := recordRequest(handler, "POST", path+"/cancel", ""); response.Body.String() != "{\"accepted\":false,\"runId\":null}\n" {
		t.Fatal(response.Body.String())
	}
	if response := recordRequest(handler, "DELETE", path, ""); response.Code != 204 || response.Body.Len() != 0 {
		t.Fatalf("delete: %d %s", response.Code, response.Body.String())
	}
	for _, path := range []string{path + "/prompts", "/unknown", "/sessions/missing/prompts"} {
		if response := recordRequest(handler, "POST", path, `{}`); response.Code != 404 {
			t.Fatalf("unknown path: %s status=%d", path, response.Code)
		}
	}
}

func TestHTTPRequestValidation(t *testing.T) {
	handler := newHTTPHandler(t, httpProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		t.Error("invalid request reached provider")
		return ProviderTurn{}, nil
	}))
	path := createHTTPSession(t, handler) + "/prompts"
	for _, scenario := range []struct {
		body, contentType string
		status            int
	}{
		{`{"message":"hi"}`, "text/plain", 415},
		{`{"message":"hi"}`, "application/json; charset=latin1", 415},
		{"\xff", "application/json", 400},
		{`{"message":`, "application/json", 400},
		{`[]`, "application/json", 400},
		{`null`, "application/json", 400},
		{`{"message":null}`, "application/json", 400},
		{`{"message":7}`, "application/json", 400},
		{`{"message":""}`, "application/json", 400},
		{`{}`, "application/json", 400},
		{strings.Repeat("x", MaxHTTPBodyBytes+1), "application/json", 413},
	} {
		request := httptest.NewRequest("POST", path, strings.NewReader(scenario.body))
		request.Header.Set("Content-Type", scenario.contentType)
		request.ContentLength = -1 // Exercise the read limit without a length header.
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != scenario.status || response.Header().Get("Content-Type") != "application/json; charset=utf-8" {
			t.Fatalf("request %.30q: status=%d body=%s", scenario.body, response.Code, response.Body.String())
		}
	}
	request := httptest.NewRequest("POST", path, nil)
	request.ContentLength = MaxHTTPBodyBytes + 1
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != 413 {
		t.Fatalf("declared oversized body: %d", response.Code)
	}
}

func TestHTTPHistoryCommitAndRollback(t *testing.T) {
	handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		message := request.History[len(request.History)-1].Text
		if message == "fail_before" {
			return ProviderTurn{}, errors.New("provider failed before streaming")
		}
		if err := emit("hello\n"); err != nil {
			return ProviderTurn{}, err
		}
		if message == "fail_during" {
			return ProviderTurn{}, errors.New("provider failed during streaming")
		}
		return httpText(fmt.Sprint(len(request.History))), nil
	}))
	path := createHTTPSession(t, handler)
	for _, scenario := range []struct{ message, status, text string }{
		{"go", "completed", "1"}, {"fail_before", "failed", ""}, {"fail_during", "failed", ""}, {"go", "completed", "3"},
	} {
		response := recordRequest(handler, "POST", path+"/prompts", `{"message":"`+scenario.message+`"}`)
		events := decodeHTTPEvents(t, response, scenario.status)
		last := events[len(events)-1]
		if scenario.text != "" && (last.Text == nil || *last.Text != scenario.text) {
			t.Fatalf("history not committed or rolled back: %#v", last)
		}
		if scenario.status == "failed" && (last.Error == nil || last.Error.Code != "provider_error") {
			t.Fatalf("provider failure: %#v", last)
		}
	}
}

func TestHTTPBusyCancelAndIndependentSessions(t *testing.T) {
	entered := make(chan struct{})
	handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		if request.History[len(request.History)-1].Text == "wait" {
			close(entered)
			<-ctx.Done()
			return ProviderTurn{}, ctx.Err()
		}
		return httpText(fmt.Sprint(len(request.History))), nil
	}))
	path := createHTTPSession(t, handler)
	done := make(chan struct{})
	var stream *httptest.ResponseRecorder
	go func() {
		stream = recordRequest(handler, "POST", path+"/prompts", `{"message":"wait"}`)
		close(done)
	}()
	awaitHTTP(t, entered)
	if response := recordRequest(handler, "POST", path+"/prompts", `{"message":"go"}`); response.Code != 409 {
		t.Fatalf("busy: %d %s", response.Code, response.Body.String())
	}
	other := createHTTPSession(t, handler)
	decodeHTTPEvents(t, recordRequest(handler, "POST", other+"/prompts", `{"message":"go"}`), "completed")
	response := recordRequest(handler, "POST", path+"/cancel", "")
	var canceled struct {
		Accepted bool
		RunID    string
	}
	if json.Unmarshal(response.Body.Bytes(), &canceled) != nil || !canceled.Accepted {
		t.Fatal(response.Body.String())
	}
	awaitHTTP(t, done)
	events := decodeHTTPEvents(t, stream, "aborted")
	if canceled.RunID != events[0].RunID {
		t.Fatal("cancel response changed run ID")
	}
	events = decodeHTTPEvents(t, recordRequest(handler, "POST", path+"/prompts", `{"message":"go"}`), "completed")
	if *events[len(events)-1].Text != "1" {
		t.Fatal("canceled run committed history")
	}
}

func TestHTTPDeleteWaitsForCleanup(t *testing.T) {
	entered, canceled, release := make(chan struct{}), make(chan struct{}), make(chan struct{})
	handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		close(entered)
		<-ctx.Done()
		close(canceled)
		<-release
		return ProviderTurn{}, ctx.Err()
	}))
	// Ensure a failed assertion cannot leave a cooperative fixture waiting forever.
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})
	path := createHTTPSession(t, handler)
	streamDone, deleteDone := make(chan struct{}), make(chan struct{})
	go func() {
		recordRequest(handler, "POST", path+"/prompts", `{"message":"wait"}`)
		close(streamDone)
	}()
	awaitHTTP(t, entered)
	var deleted *httptest.ResponseRecorder
	go func() {
		deleted = recordRequest(handler, "DELETE", path, "")
		close(deleteDone)
	}()
	awaitHTTP(t, canceled)
	select {
	case <-deleteDone:
		t.Fatal("delete acknowledged before provider cleanup")
	default:
	}
	if response := recordRequest(handler, "POST", path+"/prompts", `{"message":"go"}`); response.Code != 404 {
		t.Fatalf("closed session accepted another prompt: %d", response.Code)
	}
	close(release)
	awaitHTTP(t, deleteDone)
	awaitHTTP(t, streamDone)
	if deleted.Code != 204 {
		t.Fatalf("delete status: %d", deleted.Code)
	}
}

func TestHTTPDisconnectAndOutputFailure(t *testing.T) {
	for _, disconnect := range []bool{true, false} {
		t.Run(fmt.Sprint(disconnect), func(t *testing.T) {
			entered := make(chan struct{})
			var canceled atomic.Bool
			handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
				defer func() { canceled.Store(ctx.Err() != nil) }()
				close(entered)
				if disconnect {
					<-ctx.Done()
					return ProviderTurn{}, ctx.Err()
				}
				err := emit("one")
				return ProviderTurn{}, err
			}))
			path := createHTTPSession(t, handler)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			request := httptest.NewRequest("POST", path+"/prompts", strings.NewReader(`{"message":"go"}`)).WithContext(ctx)
			request.Header.Set("Content-Type", "application/json")
			writer := &failedHTTPWriter{ResponseRecorder: httptest.NewRecorder(), fail: !disconnect}
			done := make(chan struct{})
			go func() { handler.ServeHTTP(writer, request); close(done) }()
			awaitHTTP(t, entered)
			if disconnect {
				cancel()
			}
			awaitHTTP(t, done)
			if !canceled.Load() || handler.sessions[strings.TrimPrefix(path, "/sessions/")].state().MessageCount != 0 {
				t.Fatal("disconnect or output failure did not cancel and roll back")
			}
		})
	}
}

type failedHTTPWriter struct {
	*httptest.ResponseRecorder
	fail   bool
	writes int
}

func (writer *failedHTTPWriter) Write(data []byte) (int, error) {
	writer.writes++
	if writer.fail && writer.writes == 2 {
		return 0, io.ErrClosedPipe
	}
	return writer.ResponseRecorder.Write(data)
}

func TestHTTPOutputLimit(t *testing.T) {
	for _, stage := range []string{"delta", "tool", "terminal"} {
		t.Run(stage, func(t *testing.T) {
			handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
				large := strings.Repeat("x", MaxJSONLLineBytes)
				switch stage {
				case "delta":
					return ProviderTurn{}, emit(large)
				case "tool":
					calls := []ToolCall{{CallID: "one", Name: "missing", Arguments: json.RawMessage(`{"text":"` + large + `"}`)}}
					return ProviderTurn{Assistant: Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
				default:
					return httpText(large), nil
				}
			}))
			path := createHTTPSession(t, handler)
			response := recordRequest(handler, "POST", path+"/prompts", `{"message":"go"}`)
			for _, line := range bytes.Split(response.Body.Bytes(), []byte{'\n'}) {
				if len(line) > MaxJSONLLineBytes {
					t.Fatal("oversized output escaped")
				}
			}
			events := decodeHTTPEvents(t, response, "failed")
			if events[len(events)-1].Error.Code != "output_too_large" {
				t.Fatalf("wrong output error: %#v", events)
			}
			if handler.sessions[strings.TrimPrefix(path, "/sessions/")].state().MessageCount != 0 {
				t.Fatal("oversized run committed history")
			}
		})
	}
}

func TestHTTPShutdownCancelsAllSessions(t *testing.T) {
	entered, canceled, release := make(chan struct{}, 2), make(chan struct{}, 2), make(chan struct{})
	handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		entered <- struct{}{}
		<-ctx.Done()
		canceled <- struct{}{}
		<-release
		return ProviderTurn{}, ctx.Err()
	}))
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})
	for range 2 {
		path := createHTTPSession(t, handler)
		go recordRequest(handler, "POST", path+"/prompts", `{"message":"wait"}`)
	}
	awaitHTTP(t, entered)
	awaitHTTP(t, entered)
	done := make(chan struct{})
	go func() { handler.shutdown(); close(done) }()
	awaitHTTP(t, canceled)
	awaitHTTP(t, canceled)
	if response := recordRequest(handler, "POST", "/sessions", ""); response.Code != 503 {
		t.Fatal("shutdown accepted another session")
	}
	select {
	case <-done:
		t.Fatal("shutdown did not wait for cleanup")
	default:
	}
	close(release)
	awaitHTTP(t, done)
	if len(handler.sessions) != 0 {
		t.Fatal("shutdown retained sessions")
	}
}

func TestHTTPCancelInterruptsBlockedOutput(t *testing.T) {
	handler := newHTTPHandler(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		return ProviderTurn{}, emit("blocked")
	}))
	path := createHTTPSession(t, handler)
	writer := &blockedHTTPWriter{ResponseRecorder: httptest.NewRecorder(),
		blocked: make(chan struct{}), deadline: make(chan time.Time, 1)}
	request := httptest.NewRequest("POST", path+"/prompts", strings.NewReader(`{"message":"go"}`))
	request.Header.Set("Content-Type", "application/json")
	done := make(chan struct{})
	go func() { handler.ServeHTTP(writer, request); close(done) }()
	awaitHTTP(t, writer.blocked)
	recordRequest(handler, "POST", path+"/cancel", "")
	awaitHTTP(t, done)
	if handler.sessions[strings.TrimPrefix(path, "/sessions/")].state().MessageCount != 0 {
		t.Fatal("blocked output committed history")
	}
}

type blockedHTTPWriter struct {
	*httptest.ResponseRecorder
	blocked  chan struct{}
	deadline chan time.Time
	writes   int
}

func (writer *blockedHTTPWriter) Write(data []byte) (int, error) {
	writer.writes++
	if writer.writes == 2 {
		close(writer.blocked)
		deadline := <-writer.deadline
		<-time.After(time.Until(deadline))
		return 0, context.DeadlineExceeded
	}
	return writer.ResponseRecorder.Write(data)
}

func (writer *blockedHTTPWriter) SetWriteDeadline(deadline time.Time) error {
	if !deadline.IsZero() {
		writer.deadline <- deadline
	}
	return nil
}
