package tinyagent

import (
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strconv"
	"time"
)

// HTTPOptions controls listening and access to the in-memory session server.
type HTTPOptions struct {
	Address     string // Defaults to 127.0.0.1:8080. Port 0 selects an available port.
	Token       string
	AllowRemote bool
}

type HTTPServer struct {
	address   string
	transport *http.Server
	handler   *httpHandler
	done      chan struct{}
	err       error // Written before done closes.
}

// Serve starts an HTTP server around the same Agent used by imported callers.
// The caller owns its lifetime and must call Close when finished.
func Serve(agent *Agent, options HTTPOptions) (*HTTPServer, error) {
	if agent == nil {
		return nil, errors.New("agent is required")
	}
	address, err := listenAddress(options)
	if err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return nil, err
	}
	return startHTTPServer(agent, options.Token, listener), nil
}

// Listening is separate from serving so transport checks can use native HTTP
// over bounded connections without opening a TCP socket.
func startHTTPServer(agent *Agent, token string, listener net.Listener) *HTTPServer {
	handler := &httpHandler{agent: agent, token: token, sessions: make(map[string]*session)}
	server := &HTTPServer{
		address: (&url.URL{Scheme: "http", Host: listener.Addr().String()}).String(),
		handler: handler, done: make(chan struct{}),
		transport: &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second,
			ReadTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 64 * 1024},
	}
	go func() {
		err := server.transport.Serve(listener)
		_ = server.transport.Close()
		handler.shutdown()
		if !errors.Is(err, http.ErrServerClosed) {
			server.err = err
		}
		close(server.done)
	}()
	return server
}

func (server *HTTPServer) URL() string { return server.address }

// Wait returns once the listener has stopped and all cooperative runs have ended.
func (server *HTTPServer) Wait() error {
	<-server.done
	return server.err
}

// Close stops accepting requests, breaks blocked transport I/O, and waits for
// active providers and tools to honor cancellation. It is safe to call again.
func (server *HTTPServer) Close() error {
	server.handler.mu.Lock()
	server.handler.closing = true
	server.handler.mu.Unlock()
	err := server.transport.Close()
	return errors.Join(err, server.Wait())
}

func listenAddress(options HTTPOptions) (string, error) {
	address := options.Address
	if address == "" {
		address = "127.0.0.1:8080"
	}
	host, portText, err := net.SplitHostPort(address)
	if err != nil {
		return "", fmt.Errorf("invalid listen address: %w", err)
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 0 || port > 65535 {
		return "", errors.New("port must be an integer from 0 to 65535")
	}
	if host == "localhost" {
		host = "127.0.0.1"
	}
	ip, err := netip.ParseAddr(host)
	if err != nil {
		return "", errors.New("host must be an IP address or localhost")
	}
	for _, character := range options.Token {
		if character < 0x21 || character > 0x7e {
			return "", errors.New("token must contain only printable non-space ASCII characters")
		}
	}
	if !ip.IsLoopback() && (!options.AllowRemote || options.Token == "") {
		return "", errors.New("non-loopback binding requires AllowRemote and a bearer token")
	}
	return net.JoinHostPort(ip.String(), strconv.Itoa(port)), nil
}
