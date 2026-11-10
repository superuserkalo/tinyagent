package tinyagent_test

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"tinyagent"
)

type wireMessage struct {
	Type       string          `json:"type"`
	ID         *string         `json:"id,omitempty"`
	Command    string          `json:"command,omitempty"`
	Success    *bool           `json:"success,omitempty"`
	Data       *wireData       `json:"data,omitempty"`
	Error      *wireError      `json:"error,omitempty"`
	RunID      string          `json:"runId,omitempty"`
	Sequence   uint64          `json:"sequence,omitempty"`
	Status     string          `json:"status,omitempty"`
	Text       *string         `json:"text,omitempty"`
	Delta      string          `json:"delta,omitempty"`
	ToolCallID string          `json:"toolCallId,omitempty"`
	ToolName   string          `json:"toolName,omitempty"`
	Arguments  json.RawMessage `json:"arguments,omitempty"`
	Output     *string         `json:"output,omitempty"`
	IsError    *bool           `json:"isError,omitempty"`
}

type wireData struct {
	ProtocolVersion *int    `json:"protocolVersion,omitempty"`
	Status          string  `json:"status,omitempty"`
	RunID           *string `json:"runId,omitempty"`
	MessageCount    *int    `json:"messageCount,omitempty"`
	Accepted        *bool   `json:"accepted,omitempty"`
	AbortedRun      *bool   `json:"abortedRun,omitempty"`
}

type wireError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable *bool  `json:"retryable,omitempty"`
}

type rpcProcess struct {
	command  *exec.Cmd
	input    io.WriteCloser
	messages chan wireMessage
	done     chan struct{}
	readErr  error
	waitErr  error
	stderr   bytes.Buffer
}

func startRPC(t *testing.T, binary string) *rpcProcess {
	t.Helper()
	process := &rpcProcess{command: exec.Command(binary), messages: make(chan wireMessage, 32), done: make(chan struct{})}
	process.command.Stderr = &process.stderr
	input, err := process.command.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	process.input = input
	output, err := process.command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := process.command.Start(); err != nil {
		t.Fatal(err)
	}
	go func() {
		scanner := bufio.NewScanner(output)
		scanner.Buffer(make([]byte, 4096), tinyagent.MaxJSONLLineBytes+1)
		for scanner.Scan() {
			var message wireMessage
			decoder := json.NewDecoder(bytes.NewReader(scanner.Bytes()))
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(&message); err != nil {
				process.readErr = err
				_ = output.Close()
				break
			}
			process.messages <- message
		}
		if process.readErr == nil {
			process.readErr = scanner.Err()
		}
		close(process.messages)
		process.waitErr = process.command.Wait()
		close(process.done)
	}()
	t.Cleanup(func() {
		_ = process.input.Close()
		select {
		case <-process.done:
		case <-time.After(3 * time.Second):
			_ = process.command.Process.Kill()
			<-process.done
		}
		if process.readErr != nil || process.waitErr != nil || process.stderr.Len() != 0 {
			t.Errorf("RPC exit: read=%v wait=%v stderr=%s", process.readErr, process.waitErr, process.stderr.String())
		}
	})
	return process
}

func (process *rpcProcess) send(t *testing.T, line string) {
	t.Helper()
	if _, err := fmt.Fprintln(process.input, line); err != nil {
		t.Fatal(err)
	}
}

func (process *rpcProcess) next(t *testing.T) wireMessage {
	t.Helper()
	select {
	case message, ok := <-process.messages:
		if !ok {
			t.Fatal("RPC output ended early")
		}
		return message
	case <-time.After(3 * time.Second):
		t.Fatal("RPC response timed out")
		return wireMessage{}
	}
}

func (process *rpcProcess) until(t *testing.T, kind, status string) []wireMessage {
	t.Helper()
	var messages []wireMessage
	for {
		message := process.next(t)
		messages = append(messages, message)
		if message.Type == kind && (status == "" || message.Status == status) {
			return messages
		}
	}
}

func (process *rpcProcess) idle(t *testing.T, count int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		process.send(t, `{"id":"state","type":"get_state"}`)
		message := process.next(t)
		if message.ID == nil || *message.ID != "state" || message.Data == nil {
			t.Fatalf("expected state response: %#v", message)
		}
		if message.Data.Status == "idle" {
			if message.Data.MessageCount == nil || *message.Data.MessageCount != count {
				t.Fatalf("expected %d history messages: %#v", count, message.Data)
			}
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("session did not become idle")
}

func TestRPCSubprocess(t *testing.T) {
	directory, err := os.MkdirTemp(".", ".test-rpc-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(directory) })
	binary, err := filepath.Abs(filepath.Join(directory, "rpc"))
	if err != nil {
		t.Fatal(err)
	}
	build := exec.Command("go", "build", "-race", "-o", binary, "./testdata/rpc")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build RPC fixture: %v\n%s", err, output)
	}
	t.Run("shared static fixtures", func(t *testing.T) {
		data, err := os.ReadFile("conformance/rpc_v1.json")
		if err != nil {
			t.Fatal(err)
		}
		var fixture struct {
			Scenarios []struct {
				Commands []json.RawMessage
				Expected []wireMessage
			}
		}
		if err := json.Unmarshal(data, &fixture); err != nil {
			t.Fatal(err)
		}
		process := startRPC(t, binary)
		for _, scenario := range fixture.Scenarios[:6] {
			for _, command := range scenario.Commands {
				var compact bytes.Buffer
				if err := json.Compact(&compact, command); err != nil {
					t.Fatal(err)
				}
				process.send(t, compact.String())
			}
			for _, expected := range scenario.Expected {
				actual := process.next(t)
				if !reflect.DeepEqual(actual, expected) {
					t.Fatalf("actual=%#v expected=%#v", actual, expected)
				}
			}
		}
	})
	t.Run("completion and failed rollback", func(t *testing.T) {
		process := startRPC(t, binary)
		process.send(t, `{"id":"prompt","type":"prompt","message":"finish"}`)
		ack := process.next(t)
		if ack.Type != "response" || ack.Data == nil || ack.Data.RunID == nil {
			t.Fatalf("missing acknowledgement before events: %#v", ack)
		}
		events := process.until(t, "agent_end", "completed")
		for index, event := range events {
			if event.RunID != *ack.Data.RunID || event.Sequence != uint64(index+1) {
				t.Fatalf("invalid event identity: %#v", event)
			}
		}
		if events[1].Delta != "done\nfixture" {
			t.Fatal("embedded newline did not survive JSONL framing")
		}
		process.idle(t, 2)
		process.send(t, `{"type":"prompt","message":"fail"}`)
		process.until(t, "agent_end", "failed")
		process.idle(t, 2)
	})
	t.Run("busy abort and reset", func(t *testing.T) {
		process := startRPC(t, binary)
		process.send(t, `{"type":"prompt","message":"wait"}`)
		process.until(t, "text_delta", "")
		process.send(t, `{"id":"busy","type":"prompt","message":"second"}`)
		busy := process.next(t)
		if busy.Error == nil || busy.Error.Code != "busy" {
			t.Fatalf("missing busy rejection: %#v", busy)
		}
		process.send(t, `{"id":"abort","type":"abort"}`)
		seenAbort, seenEnd := false, false
		for !seenAbort || !seenEnd {
			message := process.next(t)
			seenAbort = seenAbort || (message.ID != nil && *message.ID == "abort" && message.Data != nil && message.Data.Accepted != nil && *message.Data.Accepted)
			seenEnd = seenEnd || message.Status == "aborted"
		}
		process.idle(t, 0)
		process.send(t, `{"type":"prompt","message":"wait"}`)
		process.until(t, "text_delta", "")
		process.send(t, `{"id":"reset","type":"new_session"}`)
		seenEnd = false
		for {
			message := process.next(t)
			seenEnd = seenEnd || message.Status == "aborted"
			if message.ID != nil && *message.ID == "reset" {
				if !seenEnd || message.Data == nil || message.Data.AbortedRun == nil || !*message.Data.AbortedRun {
					t.Fatal("reset did not await the aborted run")
				}
				break
			}
		}
		process.idle(t, 0)
	})
	t.Run("oversized output", func(t *testing.T) {
		process := startRPC(t, binary)
		process.send(t, `{"type":"prompt","message":"oversized"}`)
		messages := process.until(t, "protocol_error", "")
		if messages[len(messages)-1].Error.Code != "output_too_large" {
			t.Fatal("missing output limit error")
		}
		process.idle(t, 0)
	})
	for _, ending := range []string{"eof", "interrupt"} {
		t.Run(ending+" cancels active Bash", func(t *testing.T) {
			if ending == "interrupt" && runtime.GOOS == "windows" {
				t.Skip("os.Interrupt delivery is not supported on Windows")
			}
			process := startRPC(t, binary)
			process.send(t, `{"type":"prompt","message":"wait_tool"}`)
			process.until(t, "tool_execution_start", "")
			if ending == "eof" {
				_ = process.input.Close()
			} else if err := process.command.Process.Signal(os.Interrupt); err != nil {
				t.Fatal(err)
			}
			messages := process.until(t, "agent_end", "aborted")
			if len(messages) != 2 || messages[0].Type != "tool_execution_end" || messages[0].IsError == nil || !*messages[0].IsError {
				t.Fatalf("cancelled tool has no matching end: %#v", messages)
			}
		})
	}
	t.Run("CLI framing and fatal input", func(t *testing.T) {
		for name, input := range map[string][]byte{
			"state":          []byte("{\"id\":\"state\",\"type\":\"get_state\"}\n"),
			"invalid_json":   []byte("{\n"),
			"invalid_utf8":   {0xff, '\n'},
			"line_too_large": []byte(strings.Repeat(" ", tinyagent.MaxJSONLLineBytes+1)),
		} {
			t.Run(name, func(t *testing.T) {
				command := exec.Command(binary, "--cli", "--mode", "rpc")
				command.Stdin = bytes.NewReader(input)
				command.Env = append(os.Environ(), "OPENAI_API_KEY=", "ANTHROPIC_API_KEY=", "TINYAGENT_PROVIDER=openai", "TINYAGENT_MODEL=fixture")
				var stderr bytes.Buffer
				command.Stderr = &stderr
				output, err := command.Output()
				if err != nil || stderr.Len() != 0 {
					t.Fatalf("CLI failed: %v %s", err, stderr.String())
				}
				var message wireMessage
				if err := json.Unmarshal(bytes.TrimSpace(output), &message); err != nil {
					t.Fatal(err)
				}
				if name == "state" {
					if message.Data == nil || message.Data.Status != "idle" {
						t.Fatal("missing idle state")
					}
				} else if message.Error == nil || message.Error.Code != name {
					t.Fatalf("expected %s: %#v", name, message)
				}
			})
		}
	})
}
