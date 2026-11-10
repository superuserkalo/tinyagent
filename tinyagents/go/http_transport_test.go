package tinyagent

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"sync"
	"testing"
	"time"
)

// net.Pipe provides bounded native net.Conn I/O. net/http still owns request
// serialization, parsing, response framing, deadlines, and disconnect contexts.
type httpPipeListener struct {
	connections chan net.Conn
	closed      chan struct{}
	once        sync.Once
}

func (listener *httpPipeListener) Accept() (net.Conn, error) {
	select {
	case connection := <-listener.connections:
		return connection, nil
	case <-listener.closed:
		return nil, net.ErrClosed
	}
}

func (listener *httpPipeListener) Close() error {
	listener.once.Do(func() { close(listener.closed) })
	return nil
}

func (listener *httpPipeListener) Addr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 12345}
}

func startPipeHTTP(t *testing.T, provider Provider, wrap func(net.Conn) net.Conn, tools ...*Tool) (*HTTPServer, *http.Client) {
	t.Helper()
	agent, err := NewAgent(AgentConfig{Model: "fixture", Provider: provider, Tools: tools})
	if err != nil {
		t.Fatal(err)
	}
	listener := &httpPipeListener{connections: make(chan net.Conn), closed: make(chan struct{})}
	server := startHTTPServer(agent, "secret", listener)
	transport := &http.Transport{DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
		client, peer := net.Pipe()
		var connection net.Conn = peer
		if wrap != nil {
			connection = wrap(peer)
		}
		select {
		case listener.connections <- connection:
			return client, nil
		case <-ctx.Done():
			client.Close()
			peer.Close()
			return nil, ctx.Err()
		case <-listener.closed:
			client.Close()
			peer.Close()
			return nil, net.ErrClosed
		}
	}}
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	t.Cleanup(func() {
		transport.CloseIdleConnections()
		if err := server.Close(); err != nil {
			t.Error(err)
		}
	})
	return server, client
}

func requestHTTP(t *testing.T, client *http.Client, method, address string, body io.Reader, status int) *http.Response {
	t.Helper()
	request, err := http.NewRequest(method, address, body)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer secret")
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != status {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		response.Body.Close()
		t.Fatalf("%s %s: status=%d body=%s", method, address, response.StatusCode, data)
	}
	return response
}

func readHTTPEvent(t *testing.T, decoder *json.Decoder) Event {
	t.Helper()
	var event Event
	if err := decoder.Decode(&event); err != nil {
		t.Fatal(err)
	}
	return event
}
