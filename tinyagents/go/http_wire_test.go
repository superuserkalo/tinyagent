package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func pipeHTTPSession(t *testing.T, server *HTTPServer, client *http.Client) (string, *session) {
	t.Helper()
	response := requestHTTP(t, client, "POST", server.URL()+"/sessions", nil, 201)
	defer response.Body.Close()
	var created struct{ SessionID string }
	if err := json.NewDecoder(response.Body).Decode(&created); err != nil || created.SessionID == "" {
		t.Fatalf("session=%#v error=%v", created, err)
	}
	server.handler.mu.Lock()
	current := server.handler.sessions[created.SessionID]
	server.handler.mu.Unlock()
	return server.URL() + "/sessions/" + created.SessionID, current
}

func TestHTTPWireRejectsDuplicateAuthorization(t *testing.T) {
	server, client := startPipeHTTP(t, httpProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		t.Error("authentication probe reached provider")
		return ProviderTurn{}, nil
	}), nil)
	for _, tokens := range [][]string{{"Bearer secret", "Bearer wrong"}, {"Bearer secret", "Bearer secret"}, {"Bearer wrong", "Bearer secret"}} {
		request, _ := http.NewRequest("GET", server.URL()+"/health", nil)
		request.Header["Authorization"] = tokens
		response, err := client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		if response.StatusCode != 401 || response.Header.Get("WWW-Authenticate") != "Bearer" {
			t.Errorf("duplicate credentials accepted: tokens=%v status=%d", tokens, response.StatusCode)
		}
	}
}

type httpFailFinalChunk struct {
	net.Conn
	rejected *atomic.Int32
}

func (connection *httpFailFinalChunk) Write(data []byte) (int, error) {
	if bytes.Equal(data, []byte("0\r\n\r\n")) {
		connection.rejected.Add(1)
		connection.Conn.Close()
		return 0, io.ErrClosedPipe
	}
	return connection.Conn.Write(data)
}

func TestHTTPWireCompletionNeedsNoPostHandlerWrite(t *testing.T) {
	var rejected atomic.Int32
	server, client := startPipeHTTP(t, httpProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		return httpText("done"), nil
	}), func(connection net.Conn) net.Conn { return &httpFailFinalChunk{Conn: connection, rejected: &rejected} })
	path, current := pipeHTTPSession(t, server, client)
	response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"go"}`), 200)
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Errorf("completion framing failed after history commit: count=%d error=%v body=%s", current.state().MessageCount, err, body)
	}
	if rejected.Load() != 0 {
		t.Errorf("HTTP required a final chunk outside the handler: writes=%d", rejected.Load())
	}
	if state := current.state(); state.Status != "idle" || state.MessageCount != 2 {
		t.Fatalf("completed history=%#v", state)
	}
}

func TestHTTPWireSlowCleanupRetainsToolEvents(t *testing.T) {
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
	provider := httpProvider(func(context.Context, ProviderRequest, func(string) error) (ProviderTurn, error) {
		calls := []ToolCall{{CallID: "call", Name: "wait", Arguments: json.RawMessage(`{}`)}}
		return ProviderTurn{Assistant: Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
	})
	server, client := startPipeHTTP(t, provider, nil, tool)
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})
	path, current := pipeHTTPSession(t, server, client)
	response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"go"}`), 200)
	defer response.Body.Close()
	decoder := json.NewDecoder(response.Body)
	for _, kind := range []string{"agent_start", "tool_execution_start"} {
		if event := readHTTPEvent(t, decoder); event.Type != kind {
			t.Fatalf("event=%#v want=%s", event, kind)
		}
	}
	awaitHTTP(t, entered)
	canceled := requestHTTP(t, client, "POST", path+"/cancel", nil, 200)
	io.Copy(io.Discard, canceled.Body)
	canceled.Body.Close()
	awaitHTTP(t, cleaning)
	time.Sleep(1100 * time.Millisecond)
	if current.state().Status != "running" {
		t.Error("session finished before tool cleanup")
	}
	close(release)
	end := readHTTPEvent(t, decoder)
	if end.Type != "tool_execution_end" || end.IsError == nil || !*end.IsError {
		t.Fatalf("cancelled tool has no paired end: %#v", end)
	}
	if end := readHTTPEvent(t, decoder); end.Status != "aborted" {
		t.Fatalf("terminal event=%#v", end)
	}
	if _, err := io.Copy(io.Discard, response.Body); err != nil && !errors.Is(err, io.EOF) {
		t.Fatalf("cancellation framing=%v", err)
	}
	if current.state().MessageCount != 0 {
		t.Fatal("cancelled run committed history")
	}
}
