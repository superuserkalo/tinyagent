package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"tinyagent"
	"tinyagent/cli"
)

type provider struct{}

func (provider) Stream(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
	prompt := request.History[len(request.History)-1].Text
	switch prompt {
	case "wait":
		if err := emit("waiting"); err != nil {
			return tinyagent.ProviderTurn{}, err
		}
		<-ctx.Done()
		return tinyagent.ProviderTurn{}, ctx.Err()
	case "fail":
		if err := emit("partial"); err != nil {
			return tinyagent.ProviderTurn{}, err
		}
		return tinyagent.ProviderTurn{}, errors.New("fixture provider failure")
	case "oversized":
		return tinyagent.ProviderTurn{}, emit(strings.Repeat("x", tinyagent.MaxJSONLLineBytes))
	case "wait_tool":
		calls := []tinyagent.ToolCall{{CallID: "call", Name: "bash", Arguments: json.RawMessage(`{"command":"sleep 3"}`)}}
		return tinyagent.ProviderTurn{ToolCalls: calls, Assistant: tinyagent.Message{Role: "assistant", ToolCalls: calls}}, nil
	}
	if err := emit("done\nfixture"); err != nil {
		return tinyagent.ProviderTurn{}, err
	}
	return tinyagent.ProviderTurn{Text: "done\nfixture", Assistant: tinyagent.Message{Role: "assistant", Text: "done\nfixture"}}, nil
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var err error
	if len(os.Args) > 1 && os.Args[1] == "--cli" {
		err = cli.Run(ctx, os.Args[2:], os.Stdin, os.Stdout, os.Stderr)
	} else {
		var tool *tinyagent.Tool
		tool, err = tinyagent.BashTool()
		if err == nil {
			var agent *tinyagent.Agent
			agent, err = tinyagent.NewAgent(tinyagent.AgentConfig{Model: "fixture", Provider: provider{}, Tools: []*tinyagent.Tool{tool}})
			if err == nil {
				err = tinyagent.RunRPC(ctx, agent, os.Stdin, os.Stdout)
			}
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
