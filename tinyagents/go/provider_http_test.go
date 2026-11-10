package tinyagent_test

import (
	"context"
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"tinyagent"
)

// These tests must use TCP. A restricted runner reports failures, never skips.
func TestProvidersLocalHTTP(t *testing.T) {
	for _, scenario := range []struct {
		name string
		new  func(string) tinyagent.Provider
	}{
		{"openai", func(endpoint string) tinyagent.Provider {
			return &tinyagent.OpenAIProvider{APIKey: "fixture", Endpoint: endpoint}
		}},
		{"anthropic", func(endpoint string) tinyagent.Provider {
			return &tinyagent.AnthropicProvider{APIKey: "fixture", Endpoint: endpoint}
		}},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			stream := fixture(t, scenario.name+"_text")
			server := &http.Server{ReadHeaderTimeout: time.Second, Handler: http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				response.Header().Set("Content-Type", "text/event-stream")
				_, _ = io.WriteString(response, stream)
			})}
			go func() { _ = server.Serve(listener) }()
			t.Cleanup(func() { _ = server.Close() })
			result, err := newAgent(t, scenario.new("http://"+listener.Addr().String())).Run(
				context.Background(), "go", tinyagent.RunOptions{})
			if err != nil || result.Text != "done\ncafé" {
				t.Fatalf("result=%#v error=%v", result, err)
			}
		})
	}
}
