//go:build !windows

package cli

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"tinyagent"
)

type consoleStats struct {
	Active        int
	StreamsOpen   bool
	ModesRestored bool
	Error         string
}

// Invoked only as a separate native process by TestConsoleSubprocess.
func TestConsoleProcess(t *testing.T) {
	mode := os.Getenv("TINYAGENT_CONSOLE_FIXTURE")
	if mode == "" {
		return
	}
	directory := os.Getenv("TINYAGENT_CONSOLE_DIRECTORY")
	streams := []*os.File{os.Stdin, os.Stdout, os.Stderr}
	var modes []int
	for _, stream := range streams {
		flags, err := consoleFlags(stream, syscall.F_GETFL)
		if err != nil {
			t.Fatal(err)
		}
		modes = append(modes, flags&syscall.O_NONBLOCK)
	}
	active := 0
	provider := consoleProvider(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
		active++
		defer func() { active-- }()
		if err := os.WriteFile(filepath.Join(directory, "ready"), []byte("ready"), 0600); err != nil {
			return tinyagent.ProviderTurn{}, err
		}
		switch mode {
		case "request":
			<-ctx.Done()
			return tinyagent.ProviderTurn{}, ctx.Err()
		case "stderr":
			return tinyagent.ProviderTurn{}, errors.New(strings.Repeat("x", 512*1024))
		case "bash":
			path := "'" + strings.ReplaceAll(filepath.Join(directory, "child.pid"), "'", "'\"'\"'") + "'"
			arguments, err := json.Marshal(struct {
				Command string `json:"command"`
			}{
				Command: "sleep 30 & child=$!; printf '%s' \"$child\" > " + path + "; wait",
			})
			if err != nil {
				return tinyagent.ProviderTurn{}, err
			}
			calls := []tinyagent.ToolCall{{CallID: "bash-call", Name: "bash", Arguments: arguments}}
			return tinyagent.ProviderTurn{Assistant: tinyagent.Message{Role: "assistant", ToolCalls: calls}, ToolCalls: calls}, nil
		default:
			if err := emit(strings.Repeat("x", 512*1024)); err != nil {
				return tinyagent.ProviderTurn{}, err
			}
			return tinyagent.ProviderTurn{Text: "done", Assistant: tinyagent.Message{Role: "assistant", Text: "done"}}, nil
		}
	})
	tool, err := tinyagent.BashTool()
	if err != nil {
		t.Fatal(err)
	}
	agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{Model: "fixture", Provider: provider, Tools: []*tinyagent.Tool{tool}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	err = interactive(ctx, agent, os.Stdin, os.Stdout, os.Stderr)
	stop()
	stats := consoleStats{Active: active, StreamsOpen: true, ModesRestored: true}
	if err != nil {
		stats.Error = err.Error()
	}
	for index, stream := range streams {
		if _, err := stream.Stat(); err != nil {
			stats.StreamsOpen = false
		}
		flags, err := consoleFlags(stream, syscall.F_GETFL)
		if err != nil || flags&syscall.O_NONBLOCK != modes[index] {
			stats.ModesRestored = false
		}
	}
	data, err := json.Marshal(stats)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, "stats.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	if stats.Error != "" {
		os.Exit(1)
	}
	os.Exit(0)
}

func TestConsoleSubprocess(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"idle", "request", "stdout", "stderr", "broken", "shared", "bash"} {
		t.Run(mode, func(t *testing.T) {
			directory := consoleDirectory(t)
			command := exec.Command(executable, "-test.run=^TestConsoleProcess$")
			command.Env = append(os.Environ(), "TINYAGENT_CONSOLE_FIXTURE="+mode, "TINYAGENT_CONSOLE_DIRECTORY="+directory)
			input, err := command.StdinPipe()
			if err != nil {
				t.Fatal(err)
			}
			defer input.Close()
			stdout, stdoutWriter, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer stdout.Close()
			defer stdoutWriter.Close()
			stderr, stderrWriter, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer stderr.Close()
			defer stderrWriter.Close()
			command.Stdout, command.Stderr = stdoutWriter, stderrWriter
			if mode == "shared" {
				command.Stderr = stdoutWriter
			}
			if err := command.Start(); err != nil {
				t.Fatal(err)
			}
			stdoutWriter.Close()
			stderrWriter.Close()
			done := make(chan error, 1)
			go func() { done <- command.Wait() }()
			waited := false
			defer func() {
				if !waited {
					command.Process.Kill()
					<-done
				}
			}()
			if mode != "idle" {
				if _, err := io.WriteString(input, "go\n"); err != nil {
					t.Fatal(err)
				}
			}
			if mode == "request" || mode == "bash" {
				marker := "ready"
				if mode == "bash" {
					marker = "child.pid"
				}
				deadline := time.Now().Add(3 * time.Second)
				for {
					data, err := os.ReadFile(filepath.Join(directory, marker))
					if err == nil && len(data) != 0 {
						break
					}
					if time.Now().After(deadline) {
						t.Fatal("native fixture did not become ready")
					}
					time.Sleep(time.Millisecond)
				}
			} else {
				reader, marker := stdout, "x"
				if mode == "idle" {
					marker = "> "
				} else if mode == "stderr" {
					reader = stderr
				}
				if err := reader.SetReadDeadline(time.Now().Add(3 * time.Second)); err != nil {
					t.Fatal(err)
				}
				var received strings.Builder
				buffer := make([]byte, 128)
				for !strings.Contains(received.String(), marker) {
					n, err := reader.Read(buffer)
					if err != nil {
						t.Fatalf("waiting for native output: %v", err)
					}
					received.Write(buffer[:n])
				}
			}
			if mode == "broken" {
				stdout.Close()
			} else if err := command.Process.Signal(os.Interrupt); err != nil {
				t.Fatal(err)
			}
			select {
			case err := <-done:
				waited = true
				if (err != nil) != (mode == "broken") {
					t.Fatalf("native CLI exit: %v", err)
				}
			case <-time.After(4 * time.Second):
				t.Fatal("native CLI did not finish after interruption")
			}
			data, err := os.ReadFile(filepath.Join(directory, "stats.json"))
			if err != nil {
				t.Fatal(err)
			}
			var stats consoleStats
			if err := json.Unmarshal(data, &stats); err != nil {
				t.Fatal(err)
			}
			if stats.Active != 0 || !stats.StreamsOpen || !stats.ModesRestored || (stats.Error != "") != (mode == "broken") {
				t.Fatalf("native cleanup failed: %#v", stats)
			}
			if mode == "bash" {
				data, err := os.ReadFile(filepath.Join(directory, "child.pid"))
				if err != nil {
					t.Fatal(err)
				}
				pid, err := strconv.Atoi(string(data))
				if err != nil {
					t.Fatal(err)
				}
				if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
					t.Fatalf("Bash child survived cleanup: pid=%d error=%v", pid, err)
				}
			}
		})
	}
}
