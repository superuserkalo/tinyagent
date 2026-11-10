package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"

	"tinyagent"
)

func main() {
	echo, err := tinyagent.NewTool(tinyagent.ToolDefinition{
		Name: "echo", Description: "Return text.", Parameters: json.RawMessage(`{
			"type":"object","properties":{"text":{"type":"string"}},
			"required":["text"],"additionalProperties":false
		}`),
	}, func(ctx context.Context, raw json.RawMessage) (string, error) {
		var arguments struct {
			Text string `json:"text"`
		}
		if err := json.Unmarshal(raw, &arguments); err != nil {
			return "", err
		}
		return arguments.Text, nil
	})
	if err != nil {
		log.Fatal(err)
	}
	agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{
		Model: "gpt-5.6", Provider: &tinyagent.OpenAIProvider{}, Tools: []*tinyagent.Tool{echo},
	})
	if err != nil {
		log.Fatal(err)
	}
	result, err := agent.Run(context.Background(), "Echo hello", tinyagent.RunOptions{})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(result.Text)
}
