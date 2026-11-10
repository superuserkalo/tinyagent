package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"sync"
)

var wordPattern = regexp.MustCompile(`[a-z0-9_.-]+`)
var deniedWords = map[string]bool{
	"rm": true, "rmdir": true, "unlink": true, "shred": true, "truncate": true,
	"dd": true, "mkfs": true, "wipefs": true, "fdisk": true, "parted": true,
	"sudo": true, "doas": true, "shutdown": true, "reboot": true, "poweroff": true, "halt": true,
	"kill": true, "pkill": true, "killall": true,
}

// DeniedReason implements the CLI blacklist. It is not a command sandbox.
func DeniedReason(command string) string {
	normalized := strings.NewReplacer("\\", "", `"`, "", "'", "").Replace(strings.ToLower(command))
	words := make(map[string]bool)
	for _, word := range wordPattern.FindAllString(normalized, -1) {
		words[word] = true
	}
	var blocked []string
	for word := range words {
		if deniedWords[word] || strings.HasPrefix(word, "mkfs.") {
			blocked = append(blocked, word)
		}
	}
	if len(blocked) > 0 {
		sort.Strings(blocked)
		return fmt.Sprintf("'%s' is denied", blocked[0])
	}
	if words["find"] && words["-delete"] {
		return "'find -delete' is denied"
	}
	if words["git"] && (words["clean"] || words["restore"] || (words["reset"] && words["--hard"]) || (words["checkout"] && words["--"])) {
		return "destructive git command is denied"
	}
	if strings.Contains(strings.Join(strings.Fields(normalized), ""), ":(){:|:&};:") {
		return "fork bomb is denied"
	}
	return ""
}

func BashTool() (*Tool, error) {
	return NewTool(ToolDefinition{
		Name: "bash", Description: "Run a non-interactive bash command in the current directory.",
		Parameters: json.RawMessage(`{"type":"object","properties":{"command":{"type":"string","minLength":1}},"required":["command"],"additionalProperties":false}`),
	}, func(ctx context.Context, raw json.RawMessage) (string, error) {
		var arguments struct {
			Command string `json:"command"`
		}
		if err := json.Unmarshal(raw, &arguments); err != nil {
			return "", err
		}
		return RunBash(ctx, arguments.Command)
	})
}

// RunBash captures bounded output and cancels the process group on Unix.
func RunBash(ctx context.Context, command string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if reason := DeniedReason(command); reason != "" {
		return fmt.Sprintf("exit_code: 126\nstdout:\n\nstderr:\nblocked: %s\n", reason), nil
	}
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	capture := &bashCapture{cancel: cancel}
	cmd := exec.Command("bash", "-c", command)
	configureProcessGroup(cmd)
	cmd.Stdout = bashWriter{capture: capture}
	cmd.Stderr = bashWriter{capture: capture, stderr: true}
	if err := cmd.Start(); err != nil {
		return "", err
	}
	// Keep cancellation registered until pipes close, including when the shell
	// exits before a background descendant that inherited stdout or stderr.
	killed := make(chan struct{})
	var killErr error
	stop := context.AfterFunc(runCtx, func() {
		killErr = killProcessGroup(cmd.Process)
		close(killed)
	})
	err := cmd.Wait()
	if !stop() {
		<-killed
	}
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if capture.overflow {
		return "", errors.New("tool output exceeds 131072 UTF-8 bytes")
	}
	if killErr != nil {
		return "", fmt.Errorf("cannot stop bash: %w", killErr)
	}
	var exitError *exec.ExitError
	if err != nil && !errors.As(err, &exitError) {
		return "", err
	}
	observation := fmt.Sprintf("exit_code: %d\nstdout:\n%sstderr:\n%s", cmd.ProcessState.ExitCode(), capture.stdout.String(), capture.stderr.String())
	if len(observation) > MaxToolOutputBytes {
		return "", errors.New("tool output exceeds 131072 UTF-8 bytes")
	}
	return observation, nil
}

type bashCapture struct {
	mu       sync.Mutex
	stdout   bytes.Buffer
	stderr   bytes.Buffer
	bytes    int
	overflow bool
	cancel   context.CancelFunc
}

type bashWriter struct {
	capture *bashCapture
	stderr  bool
}

func (writer bashWriter) Write(data []byte) (int, error) {
	capture := writer.capture
	capture.mu.Lock()
	defer capture.mu.Unlock()
	if capture.bytes+len(data) > MaxToolOutputBytes {
		capture.overflow = true
		capture.cancel()
	}
	if !capture.overflow {
		capture.bytes += len(data)
		if writer.stderr {
			_, _ = capture.stderr.Write(data)
		} else {
			_, _ = capture.stdout.Write(data)
		}
	}
	return len(data), nil
}
