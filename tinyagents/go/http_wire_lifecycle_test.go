package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"
)

type httpHeldTerminal struct {
	net.Conn
	entered chan struct{}
	release chan struct{}
	once    *sync.Once
	fail    bool
}

func (connection *httpHeldTerminal) Write(data []byte) (int, error) {
	if bytes.Contains(data, []byte(`"text":"answer"`)) {
		connection.once.Do(func() { close(connection.entered) })
		<-connection.release
		if connection.fail {
			connection.Conn.Close()
			return 0, io.ErrClosedPipe
		}
	}
	return connection.Conn.Write(data)
}

func TestHTTPWireFinalDeliveryOwnsHistory(t *testing.T) {
	for _, ending := range []string{"complete", "abort", "failure"} {
		t.Run(ending, func(t *testing.T) {
			entered, release := make(chan struct{}), make(chan struct{})
			var once sync.Once
			server, client := startPipeHTTP(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
				return httpText(request.History[len(request.History)-1].Text), nil
			}), func(connection net.Conn) net.Conn {
				return &httpHeldTerminal{Conn: connection, entered: entered, release: release, once: &once, fail: ending == "failure"}
			})
			t.Cleanup(func() {
				select {
				case <-release:
				default:
					close(release)
				}
			})
			path, current := pipeHTTPSession(t, server, client)
			seed := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"seed"}`), 200)
			if _, err := io.Copy(io.Discard, seed.Body); err != nil {
				t.Fatal(err)
			}
			seed.Body.Close()
			response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"answer"}`), 200)
			defer response.Body.Close()
			awaitHTTP(t, entered)
			if state := current.state(); state.Status != "running" || state.MessageCount != 2 {
				t.Fatalf("pending delivery committed history: %#v", state)
			}
			current.mu.Lock()
			run := current.active
			current.mu.Unlock()
			if ending == "abort" {
				canceled := requestHTTP(t, client, "POST", path+"/cancel", nil, 200)
				io.Copy(io.Discard, canceled.Body)
				canceled.Body.Close()
			}
			close(release)
			body, err := io.ReadAll(response.Body)
			awaitHTTP(t, run.done)
			want := 2
			if ending == "complete" {
				want = 4
			}
			if state := current.state(); state.Status != "idle" || state.MessageCount != want {
				t.Fatalf("history=%#v want=%d", state, want)
			}
			terminals := bytes.Count(body, []byte(`"type":"agent_end"`))
			if ending == "failure" {
				if terminals != 0 {
					t.Fatalf("failed terminal write appeared in response: %s", body)
				}
			} else if err != nil || terminals != 1 {
				t.Fatalf("terminal delivery: count=%d error=%v body=%s", terminals, err, body)
			}
		})
	}
}

type httpObserveLargeWrite struct {
	net.Conn
	entered chan struct{}
	once    *sync.Once
}

func (connection *httpObserveLargeWrite) Write(data []byte) (int, error) {
	if len(data) > 8192 {
		connection.once.Do(func() { close(connection.entered) })
	}
	return connection.Conn.Write(data)
}

func TestHTTPWireStopsStalledOutput(t *testing.T) {
	for _, action := range []string{"cancel", "delete", "disconnect", "shutdown"} {
		t.Run(action, func(t *testing.T) {
			entered, cleaned := make(chan struct{}), make(chan struct{})
			var once sync.Once
			server, client := startPipeHTTP(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
				defer close(cleaned)
				return ProviderTurn{}, emit(strings.Repeat("x", 512*1024))
			}), func(connection net.Conn) net.Conn {
				return &httpObserveLargeWrite{Conn: connection, entered: entered, once: &once}
			})
			path, current := pipeHTTPSession(t, server, client)
			response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"go"}`), 200)
			defer response.Body.Close()
			decoder := json.NewDecoder(response.Body)
			if event := readHTTPEvent(t, decoder); event.Type != "agent_start" {
				t.Fatal(event)
			}
			awaitHTTP(t, entered)
			select {
			case <-cleaned:
				t.Fatal("fixture did not block in native output")
			default:
			}
			current.mu.Lock()
			run := current.active
			current.mu.Unlock()
			switch action {
			case "cancel", "delete":
				method, address, status := "POST", path+"/cancel", 200
				if action == "delete" {
					method, address, status = "DELETE", path, 204
				}
				stopped := requestHTTP(t, client, method, address, nil, status)
				io.Copy(io.Discard, stopped.Body)
				stopped.Body.Close()
			case "disconnect":
				response.Body.Close()
			case "shutdown":
				if err := server.Close(); err != nil {
					t.Fatal(err)
				}
			}
			awaitHTTP(t, cleaned)
			awaitHTTP(t, run.done)
			if state := current.state(); state.Status != "idle" || state.MessageCount != 0 {
				t.Fatalf("stalled output committed history: %#v", state)
			}
		})
	}
}

func TestHTTPWireJoinsProviderCleanup(t *testing.T) {
	for _, action := range []string{"disconnect", "delete", "shutdown"} {
		t.Run(action, func(t *testing.T) {
			entered, cleaning, release := make(chan struct{}, 2), make(chan struct{}, 2), make(chan struct{})
			server, client := startPipeHTTP(t, httpProvider(func(ctx context.Context, request ProviderRequest, emit func(string) error) (ProviderTurn, error) {
				entered <- struct{}{}
				<-ctx.Done()
				cleaning <- struct{}{}
				<-release
				return ProviderTurn{}, ctx.Err()
			}), nil)
			t.Cleanup(func() {
				select {
				case <-release:
				default:
					close(release)
				}
			})
			count := 1
			if action == "shutdown" {
				count = 2
			}
			var streams []*http.Response
			var runs []*sessionRun
			var reads []<-chan struct{}
			var path string
			for range count {
				var current *session
				path, current = pipeHTTPSession(t, server, client)
				response := requestHTTP(t, client, "POST", path+"/prompts", strings.NewReader(`{"message":"go"}`), 200)
				streams = append(streams, response)
				defer response.Body.Close()
				decoder := json.NewDecoder(response.Body)
				if event := readHTTPEvent(t, decoder); event.Type != "agent_start" {
					t.Fatal(event)
				}
				awaitHTTP(t, entered)
				current.mu.Lock()
				runs = append(runs, current.active)
				current.mu.Unlock()
				readDone := make(chan struct{})
				reads = append(reads, readDone)
				go func() {
					io.Copy(io.Discard, response.Body)
					close(readDone)
				}()
			}
			stopped := make(chan error, 1)
			go func() {
				switch action {
				case "disconnect":
					stopped <- streams[0].Body.Close()
				case "shutdown":
					stopped <- server.Close()
				case "delete":
					request, _ := http.NewRequest("DELETE", path, nil)
					request.Header.Set("Authorization", "Bearer secret")
					response, err := client.Do(request)
					if err == nil {
						response.Body.Close()
						if response.StatusCode != 204 {
							err = fmt.Errorf("delete status=%d", response.StatusCode)
						}
					}
					stopped <- err
				}
			}()
			for range count {
				awaitHTTP(t, cleaning)
			}
			for _, run := range runs {
				select {
				case <-run.done:
					t.Error("run returned before provider cleanup")
				default:
				}
			}
			if action != "disconnect" {
				select {
				case err := <-stopped:
					t.Fatalf("%s returned before cleanup: %v", action, err)
				default:
				}
			}
			close(release)
			for _, run := range runs {
				awaitHTTP(t, run.done)
			}
			if err := <-stopped; err != nil {
				t.Fatal(err)
			}
			for _, read := range reads {
				awaitHTTP(t, read)
			}
		})
	}
}
