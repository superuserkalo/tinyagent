// Package cli connects terminal I/O to the native TinyAgent core.
package cli

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"

	"tinyagent"
	"tinyagent/internal/nativeio"
)

const instructions = `You are TinyAgent, a coding agent in the current directory.
Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.`

func Run(ctx context.Context, args []string, input io.ReadCloser, output, diagnostics io.Writer) error {
	if len(args) == 1 && args[0] == "--self-test" {
		if tinyagent.DeniedReason("printf 'hello\\n'") != "" {
			return errors.New("safe command was denied")
		}
		for _, command := range []string{"rm -rf /", `r""m -rf /`, "sudo echo hi", "git reset --hard HEAD", "find . -delete", "mkfs.ext4 /dev/sda"} {
			if tinyagent.DeniedReason(command) == "" {
				return fmt.Errorf("unsafe command was allowed: %s", command)
			}
		}
		_, err := fmt.Fprintln(output, "self-test passed")
		return err
	}
	rpc := len(args) == 2 && args[0] == "--mode" && args[1] == "rpc"
	serve := len(args) > 0 && args[0] == "serve"
	if len(args) > 0 && !rpc && !serve {
		return errors.New("usage: tinyagent [--self-test | --mode rpc | serve]")
	}
	var provider tinyagent.Provider
	model := os.Getenv("TINYAGENT_MODEL")
	switch kind := os.Getenv("TINYAGENT_PROVIDER"); kind {
	case "", "openai":
		provider = &tinyagent.OpenAIProvider{}
		if model == "" {
			model = "gpt-5.6"
		}
	case "anthropic":
		provider = &tinyagent.AnthropicProvider{}
		if model == "" {
			model = "claude-sonnet-4-6"
		}
	default:
		return fmt.Errorf("unknown provider: %s", kind)
	}
	tool, err := tinyagent.BashTool()
	if err != nil {
		return err
	}
	agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{
		Model: model, Instructions: instructions, Provider: provider, Tools: []*tinyagent.Tool{tool},
	})
	if err != nil {
		return err
	}
	if rpc {
		return tinyagent.RunRPC(ctx, agent, input, output)
	}
	if serve {
		return serveHTTP(ctx, agent, args[1:], diagnostics)
	}
	return interactive(ctx, agent, input, output, diagnostics)
}

func serveHTTP(ctx context.Context, agent *tinyagent.Agent, args []string, diagnostics io.Writer) error {
	flags := flag.NewFlagSet("tinyagent serve", flag.ContinueOnError)
	flags.SetOutput(diagnostics)
	host := flags.String("host", "127.0.0.1", "listen IP address")
	port := flags.Int("port", 8080, "listen port; 0 chooses an available port")
	token := flags.String("token", os.Getenv("TINYAGENT_TOKEN"), "bearer token, defaults to TINYAGENT_TOKEN")
	remote := flags.Bool("allow-remote", false, "allow a non-loopback host with a token")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("serve does not accept positional arguments")
	}
	server, err := tinyagent.Serve(agent, tinyagent.HTTPOptions{
		Address: net.JoinHostPort(*host, strconv.Itoa(*port)), Token: *token, AllowRemote: *remote,
	})
	if err != nil {
		return err
	}
	defer server.Close()
	if _, err := fmt.Fprintln(diagnostics, "TinyAgent listening on", server.URL()); err != nil {
		return err
	}
	stop := context.AfterFunc(ctx, func() { _ = server.Close() })
	defer stop()
	return server.Wait()
}

func interactive(ctx context.Context, agent *tinyagent.Agent, input io.ReadCloser, output, diagnostics io.Writer) (runErr error) {
	if ctx.Err() != nil {
		return nil
	}
	inputFile, _ := input.(*os.File)
	outputFile, _ := output.(*os.File)
	diagnosticFile, _ := diagnostics.(*os.File)
	files, closeConsole, err := nativeio.Open(ctx, []*os.File{inputFile, outputFile, diagnosticFile})
	if err != nil {
		return err
	}
	defer func() {
		if ctx.Err() != nil && (errors.Is(runErr, os.ErrClosed) || errors.Is(runErr, ctx.Err())) {
			runErr = nil
		}
		if err := closeConsole(); err != nil {
			runErr = errors.Join(runErr, err)
		}
	}()
	if files[0] != nil {
		input = files[0]
	} else {
		stopped := make(chan struct{})
		stop := context.AfterFunc(ctx, func() {
			_ = input.Close()
			close(stopped)
		})
		defer func() {
			if !stop() {
				<-stopped
			}
		}()
	}
	if files[1] != nil {
		output = files[1]
	}
	if files[2] != nil {
		diagnostics = files[2]
	}
	reader := bufio.NewScanner(input)
	reader.Buffer(make([]byte, 4096), tinyagent.MaxJSONLLineBytes)
	var history []tinyagent.Message
	if _, err := fmt.Fprintln(output, "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit."); err != nil {
		return err
	}
	for {
		if _, err := fmt.Fprint(output, "\n> "); err != nil {
			return err
		}
		if !reader.Scan() {
			if ctx.Err() != nil {
				return nil
			}
			return reader.Err()
		}
		prompt := strings.TrimSpace(reader.Text())
		if prompt == "" {
			continue
		}
		streamed := false
		var writeErr error
		result, err := agent.Run(ctx, prompt, tinyagent.RunOptions{History: history, OnEvent: func(event tinyagent.Event) error {
			switch event.Type {
			case "text_delta":
				streamed = true
				_, writeErr = fmt.Fprint(output, event.Delta)
			case "tool_execution_start":
				var arguments struct{ Command string }
				if json.Unmarshal(event.Arguments, &arguments) == nil && arguments.Command != "" {
					_, writeErr = fmt.Fprintf(diagnostics, "\n$ %s\n", arguments.Command)
				}
			case "tool_execution_end":
				streamed = false
				if event.Output != nil {
					_, writeErr = fmt.Fprintln(diagnostics, *event.Output)
				}
			}
			return writeErr
		}})
		if writeErr != nil {
			return writeErr
		}
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			if _, err := fmt.Fprintln(diagnostics, "error:", err); err != nil {
				return err
			}
			continue
		}
		history = result.History
		if streamed {
			_, err = fmt.Fprintln(output)
		} else {
			_, err = fmt.Fprintln(output, result.Text)
		}
		if err != nil {
			return err
		}
	}
}
