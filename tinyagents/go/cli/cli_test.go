package cli

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"

	"tinyagent"
)

func TestServeOptions(t *testing.T) {
	t.Setenv("TINYAGENT_PROVIDER", "openai")
	t.Setenv("TINYAGENT_TOKEN", "")
	for _, scenario := range []struct {
		args []string
		want string
	}{
		{[]string{"serve", "--help"}, ""},
		{[]string{"serve", "--host", "0.0.0.0"}, "non-loopback binding"},
		{[]string{"serve", "--allow-remote", "--host", "0.0.0.0"}, "non-loopback binding"},
		{[]string{"serve", "--port", "65536"}, "port must be"},
		{[]string{"serve", "unexpected"}, "positional arguments"},
	} {
		var output, diagnostics bytes.Buffer
		err := Run(context.Background(), scenario.args, io.NopCloser(strings.NewReader("")), &output, &diagnostics)
		if scenario.want == "" {
			if err != nil || !strings.Contains(diagnostics.String(), "Usage of tinyagent serve") {
				t.Fatalf("help: error=%v diagnostics=%s", err, diagnostics.String())
			}
		} else if err == nil || !strings.Contains(err.Error(), scenario.want) {
			t.Fatalf("args=%v error=%v", scenario.args, err)
		}
		if output.Len() != 0 {
			t.Fatal("serve diagnostics reached stdout")
		}
	}
}

type cliProvider struct{ requests int }

func (provider *cliProvider) Stream(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
	provider.requests++
	message := request.History[len(request.History)-1].Text
	if message == "fail" {
		return tinyagent.ProviderTurn{}, errors.New("fixture failure")
	}
	if message == "second" && len(request.History) != 3 {
		return tinyagent.ProviderTurn{}, errors.New("failed turn changed CLI history")
	}
	if err := emit(message); err != nil {
		return tinyagent.ProviderTurn{}, err
	}
	return tinyagent.ProviderTurn{Text: message, Assistant: tinyagent.Message{Role: "assistant", Text: message}}, nil
}

func TestInteractiveCommitsOnlySuccessfulRuns(t *testing.T) {
	provider := &cliProvider{}
	agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{Model: "fixture", Provider: provider})
	if err != nil {
		t.Fatal(err)
	}
	var output, diagnostics bytes.Buffer
	err = interactive(context.Background(), agent, io.NopCloser(strings.NewReader("first\nfail\nsecond\n")), &output, &diagnostics)
	if err != nil || provider.requests != 3 || diagnostics.String() != "error: fixture failure\n" {
		t.Fatalf("error=%v requests=%d diagnostics=%s", err, provider.requests, diagnostics.String())
	}
	if !strings.Contains(output.String(), "> first\n") || !strings.Contains(output.String(), "> second\n") {
		t.Fatal(output.String())
	}
}
