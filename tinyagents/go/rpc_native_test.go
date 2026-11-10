//go:build !windows

package tinyagent_test

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"tinyagent"
)

func TestRPCNativeOutputCancellation(t *testing.T) {
	for _, blocked := range []string{"reply", "ack", "event"} {
		for _, ending := range []string{"eof", "cancel", "abort", "reset"} {
			if blocked == "reply" && (ending == "abort" || ending == "reset") {
				continue // No active run to cancel; EOF still bounds connection drain.
			}
			t.Run(blocked+"/"+ending, func(t *testing.T) {
				input, commands, err := os.Pipe()
				if err != nil {
					t.Fatal(err)
				}
				defer input.Close()
				defer commands.Close()
				responses, output, err := os.Pipe()
				if err != nil {
					t.Fatal(err)
				}
				defer responses.Close()
				defer output.Close()
				var started, cleaned atomic.Int32
				provider := providerFunc(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
					started.Add(1)
					defer cleaned.Add(1)
					return tinyagent.ProviderTurn{}, emit(strings.Repeat("x", 512*1024))
				})
				agent := newAgent(t, provider)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				done := make(chan error, 1)
				go func() { done <- tinyagent.RunRPC(ctx, agent, input, output) }()
				command := "prompt"
				id := "first"
				if blocked != "event" {
					id = strings.Repeat("x", 512*1024)
				}
				if blocked == "reply" {
					command = "get_state"
				}
				line, _ := json.Marshal(map[string]string{"id": id, "type": command, "message": "go"})
				if _, err := fmt.Fprintln(commands, string(line)); err != nil {
					t.Fatal(err)
				}
				reader := bufio.NewReader(responses)
				if blocked == "event" {
					for range 2 { // Prompt acknowledgement and agent_start.
						if _, err := reader.ReadBytes('\n'); err != nil {
							t.Fatal(err)
						}
					}
				}
				if _, err := io.ReadFull(reader, make([]byte, 128)); err != nil {
					t.Fatal(err)
				}
				switch ending {
				case "eof":
					commands.Close()
				case "cancel":
					cancel()
				case "abort":
					fmt.Fprintln(commands, `{"type":"abort"}`)
				case "reset":
					fmt.Fprintln(commands, `{"type":"new_session"}`)
				}
				select {
				case err := <-done:
					if err == nil {
						t.Error("stalled connection returned no transport error")
					}
				case <-time.After(2500 * time.Millisecond):
					// Release and join a failed implementation before reporting it.
					responses.Close()
					commands.Close()
					cancel()
					<-done
					t.Fatal("RPC did not release its blocked output")
				}
				want := int32(0)
				if blocked == "event" {
					want = 1
				}
				if started.Load() != want || cleaned.Load() != want {
					t.Errorf("provider started=%d cleaned=%d want=%d", started.Load(), cleaned.Load(), want)
				}
				for _, file := range []*os.File{input, output} {
					if _, err := file.Stat(); err != nil {
						t.Errorf("RPC closed a caller-owned file: %v", err)
					}
				}
			})
		}
	}
}
