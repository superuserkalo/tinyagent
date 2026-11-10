//go:build !windows

package tinyagent_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRPCProcessBackpressure(t *testing.T) {
	directory, err := os.MkdirTemp(".", ".test-rpc-output-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(directory) })
	binary, err := filepath.Abs(filepath.Join(directory, "tinyagent"))
	if err != nil {
		t.Fatal(err)
	}
	build := exec.Command("go", "build", "-race", "-o", binary, "./cmd/tinyagent")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build actual CLI: %v\n%s", err, output)
	}
	for _, scenario := range []struct{ command, ending string }{
		{"get_state", "eof"}, {"get_state", "interrupt"}, {"get_state", "broken_output"}, {"get_state", "overflow"},
		{"prompt", "eof"}, {"prompt", "interrupt"}, {"prompt", "abort"}, {"prompt", "reset"},
	} {
		t.Run(scenario.command+"/"+scenario.ending, func(t *testing.T) {
			command := exec.Command(binary, "--mode", "rpc")
			command.Env = append(os.Environ(), "OPENAI_API_KEY=", "ANTHROPIC_API_KEY=", "TINYAGENT_PROVIDER=openai", "TINYAGENT_MODEL=fixture")
			var stderr bytes.Buffer
			command.Stderr = &stderr
			input, err := command.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			defer input.Close()
			output, err := command.StdoutPipe()
			if err != nil {
				t.Fatal(err)
			}
			defer output.Close()
			if err := command.Start(); err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() { done <- command.Wait() }()
			waited := false
			defer func() {
				if !waited {
					command.Process.Kill()
					<-done
				}
			}()
			line, _ := json.Marshal(map[string]string{
				"type": scenario.command, "id": strings.Repeat("x", 512*1024), "message": "go",
			})
			if _, err := fmt.Fprintln(input, string(line)); err != nil {
				t.Fatal(err)
			}
			if err := output.(*os.File).SetReadDeadline(time.Now().Add(3 * time.Second)); err != nil {
				t.Fatal(err)
			}
			if _, err := io.ReadFull(output, make([]byte, 128)); err != nil {
				t.Fatal(err)
			}
			var floodDone chan struct{}
			switch scenario.ending {
			case "eof":
				input.Close()
			case "interrupt":
				if err := command.Process.Signal(os.Interrupt); err != nil {
					t.Fatal(err)
				}
			case "broken_output":
				output.Close()
			case "abort":
				fmt.Fprintln(input, `{"type":"abort"}`)
			case "reset":
				fmt.Fprintln(input, `{"type":"new_session"}`)
			case "overflow":
				floodDone = make(chan struct{})
				go func() {
					defer close(floodDone)
					for range 16 {
						if _, err := fmt.Fprintln(input, string(line)); err != nil {
							return
						}
					}
				}()
				defer func() { input.Close(); <-floodDone }()
				// Separate feeding the race-instrumented process from its shutdown
				// deadline. Overflow closes input and releases the blocked flood.
				select {
				case <-floodDone:
				case <-time.After(10 * time.Second):
					t.Fatal("RPC input did not stop after output queue overflow")
				}
			}
			select {
			case err := <-done:
				waited = true
				if err == nil || command.ProcessState.ExitCode() != 1 {
					t.Fatalf("stalled RPC exit=%v stderr=%s", err, stderr.String())
				}
				want := "RPC output did not drain"
				if scenario.ending == "overflow" {
					want = "RPC output queue is full"
				} else if scenario.ending == "broken_output" {
					want = "broken pipe"
				}
				if !strings.Contains(stderr.String(), want) {
					t.Errorf("missing transport failure %q: %s", want, stderr.String())
				}
			case <-time.After(4 * time.Second):
				t.Fatal("actual CLI remained blocked after cancellation")
			}
		})
	}
}
