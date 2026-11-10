package tinyagent

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// These tests intentionally fail when the host cannot bind TCP. In-memory
// handler tests do not establish transport framing or disconnect behavior.
func startTCPServer(t *testing.T, provider Provider, tools ...*Tool) *HTTPServer {
	t.Helper()
	agent, err := NewAgent(AgentConfig{Model: "fixture", Provider: provider, Tools: tools})
	if err != nil {
		t.Fatal(err)
	}
	server, err := Serve(agent, HTTPOptions{Address: "127.0.0.1:0", Token: "secret"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := server.Close(); err != nil {
			t.Error(err)
		}
	})
	return server
}

func tcpSession(t *testing.T, client *http.Client, server *HTTPServer) string {
	t.Helper()
	response := requestHTTP(t, client, "POST", server.URL()+"/sessions", nil, 201)
	defer response.Body.Close()
	var session struct{ SessionID string }
	if err := json.NewDecoder(response.Body).Decode(&session); err != nil || session.SessionID == "" {
		t.Fatalf("session=%#v error=%v", session, err)
	}
	return server.URL() + "/sessions/" + session.SessionID
}

func TestHTTPServerTCP(t *testing.T) {
	cleaned := make(chan struct{}, 8)
	bash, err := BashTool()
	if err != nil {
		t.Fatal(err)
	}
	server := startTCPServer(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		message := request.History[len(request.History)-1].Text
		if message == "wait" {
			defer func() { cleaned <- struct{}{} }()
			if err := emit("waiting"); err != nil {
				return ProviderTurn{}, err
			}
			<-ctx.Done()
			return ProviderTurn{}, ctx.Err()
		}
		if message == "bash" {
			calls := []ToolCall{{CallID: "sleep", Name: "bash", Arguments: json.RawMessage(`{"command":"sleep 30"}`)}}
			return ProviderTurn{Assistant: Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
		}
		if err := emit("hello\n"); err != nil {
			return ProviderTurn{}, err
		}
		return httpText(fmt.Sprint(len(request.History))), nil
	}), bash)
	client := &http.Client{Timeout: 5 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	path := tcpSession(t, client, server)
	t.Run("authentication and health", func(t *testing.T) {
		response, err := client.Get(server.URL() + "/health")
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != 401 {
			t.Fatal(response.StatusCode)
		}
		response = requestHTTP(t, client, "GET", server.URL()+"/health", nil, 200)
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		if err != nil || string(body) != "{\"status\":\"ok\",\"protocolVersion\":1}\n" || response.Header.Get("Access-Control-Allow-Origin") != "" {
			t.Fatalf("health body=%s error=%v headers=%v", body, err, response.Header)
		}
	})
	t.Run("chunked body and streaming", func(t *testing.T) {
		body := io.NopCloser(strings.NewReader(`{"message":"go"}`))
		response := requestHTTP(t, client, "POST", path+"/prompts", body, 200)
		defer response.Body.Close()
		if response.Header.Get("Content-Type") != "application/jsonl; charset=utf-8" {
			t.Fatal(response.Header)
		}
		decoder := json.NewDecoder(response.Body)
		start, delta, end := readHTTPEvent(t, decoder), readHTTPEvent(t, decoder), readHTTPEvent(t, decoder)
		if start.Type != "agent_start" || delta.Delta != "hello\n" || end.Status != "completed" || *end.Text != "1" || end.Sequence != 3 {
			t.Fatalf("stream: %#v %#v %#v", start, delta, end)
		}
		var extra Event
		if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
			t.Fatalf("terminal framing: %v", err)
		}
	})
	t.Run("body limits and invalid UTF-8", func(t *testing.T) {
		for _, scenario := range []struct {
			body   string
			status int
		}{{strings.Repeat("x", MaxHTTPBodyBytes+1), 413}, {"\xff", 400}} {
			response := requestHTTP(t, client, "POST", path+"/prompts", io.NopCloser(strings.NewReader(scenario.body)), scenario.status)
			response.Body.Close()
		}
	})
	for _, action := range []string{"cancel", "delete", "disconnect", "bash"} {
		t.Run(action, func(t *testing.T) {
			path := tcpSession(t, client, server)
			message := "wait"
			if action == "bash" {
				message = "bash"
			}
			response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"`+message+`"}`), 200)
			defer response.Body.Close()
			decoder := json.NewDecoder(response.Body)
			start := readHTTPEvent(t, decoder)
			active := readHTTPEvent(t, decoder)
			if action == "bash" && active.Type != "tool_execution_start" {
				t.Fatalf("Bash not started: %#v", active)
			}
			busy := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"go"}`), 409)
			busy.Body.Close()
			if action == "disconnect" {
				response.Body.Close()
				awaitHTTP(t, cleaned)
				return
			}
			if action == "delete" {
				deleted := requestHTTP(t, client, "DELETE", path, nil, 204)
				deleted.Body.Close()
			} else {
				cancel := requestHTTP(t, client, "POST", path+"/cancel", nil, 200)
				var accepted struct {
					Accepted bool
					RunID    string
				}
				err := json.NewDecoder(cancel.Body).Decode(&accepted)
				cancel.Body.Close()
				if err != nil || !accepted.Accepted || accepted.RunID != start.RunID {
					t.Fatalf("cancel response=%#v error=%v", accepted, err)
				}
			}
			if action == "bash" && readHTTPEvent(t, decoder).Type != "tool_execution_end" {
				t.Fatal("canceled Bash did not emit its end event")
			}
			if end := readHTTPEvent(t, decoder); end.Status != "aborted" || end.RunID != start.RunID {
				t.Fatalf("terminal event: %#v", end)
			}
			if action != "bash" {
				awaitHTTP(t, cleaned)
			}
		})
	}
	t.Run("server shutdown", func(t *testing.T) {
		var streams []*http.Response
		for range 2 {
			path := tcpSession(t, client, server)
			response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"wait"}`), 200)
			streams = append(streams, response)
			defer response.Body.Close()
			decoder := json.NewDecoder(response.Body)
			readHTTPEvent(t, decoder)
			readHTTPEvent(t, decoder)
		}
		if err := server.Close(); err != nil {
			t.Fatal(err)
		}
		awaitHTTP(t, cleaned)
		awaitHTTP(t, cleaned)
		for _, response := range streams {
			_, _ = io.Copy(io.Discard, response.Body)
		}
	})
}

func TestHTTPBackpressureTCP(t *testing.T) {
	var emitted atomic.Int64
	cleaned := make(chan struct{})
	server := startTCPServer(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
		defer close(cleaned)
		for {
			emitted.Add(1)
			if err := emit(strings.Repeat("x", 128*1024)); err != nil {
				return ProviderTurn{}, err
			}
		}
	}))
	client := &http.Client{Timeout: 4 * time.Second}
	t.Cleanup(client.CloseIdleConnections)
	path := tcpSession(t, client, server)
	address := strings.TrimPrefix(server.URL(), "http://")
	connection, err := net.DialTimeout("tcp", address, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	if connection, ok := connection.(*net.TCPConn); ok {
		if err := connection.SetReadBuffer(1024); err != nil {
			t.Fatal(err)
		}
	}
	request, _ := http.NewRequest("POST", path+"/prompts", strings.NewReader(`{"message":"go"}`))
	request.Header.Set("Authorization", "Bearer secret")
	request.Header.Set("Content-Type", "application/json")
	if err := request.Write(connection); err != nil {
		t.Fatal(err)
	}
	if err := connection.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), request)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != 200 {
		t.Fatal(response.StatusCode)
	}
	// Stop reading and wait until the provider is held inside a synchronous write.
	blocked := false
	for range 20 {
		before := emitted.Load()
		time.Sleep(100 * time.Millisecond)
		if before > 0 && emitted.Load() == before {
			blocked = true
			break
		}
	}
	if !blocked {
		t.Fatal("fixture did not reach network backpressure")
	}
	cancel := requestHTTP(t, client, "POST", path+"/cancel", nil, 200)
	cancel.Body.Close()
	awaitHTTP(t, cleaned)
	deleted := requestHTTP(t, client, "DELETE", path, nil, 204)
	deleted.Body.Close()
}
