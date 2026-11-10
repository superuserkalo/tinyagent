package tinyagent_test

import (
	"context"
	"errors"
	"runtime"
	"strings"
	"testing"
	"time"

	"tinyagent"
)

func TestBashBlacklistAndOutput(t *testing.T) {
	for _, command := range []string{"rm -rf /", `r""m -rf /`, "sudo echo hi", "git reset --hard HEAD", "find . -delete", "mkfs.ext4 /dev/sda"} {
		if tinyagent.DeniedReason(command) == "" {
			t.Errorf("allowed unsafe command %q", command)
		}
	}
	output, err := tinyagent.RunBash(context.Background(), "printf 'hello\\n'; printf 'warning\\n' >&2; exit 7")
	if err != nil || output != "exit_code: 7\nstdout:\nhello\nstderr:\nwarning\n" {
		t.Fatalf("output=%q error=%v", output, err)
	}
	blocked, err := tinyagent.RunBash(context.Background(), "rm something")
	if err != nil || !strings.Contains(blocked, "exit_code: 126") {
		t.Fatalf("blocked=%q error=%v", blocked, err)
	}
}

func TestBashOutputLimit(t *testing.T) {
	output, err := tinyagent.RunBash(context.Background(), "while true; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done")
	if err == nil || !strings.Contains(err.Error(), "output exceeds") || output != "" {
		t.Fatalf("output=%q error=%v", output, err)
	}
}

func TestBashCancellationIncludesDescendants(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix process-group contract")
	}
	for _, command := range []string{"sleep 3", "sleep 3 &"} {
		t.Run(command, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			timer := time.AfterFunc(100*time.Millisecond, cancel)
			defer timer.Stop()
			started := time.Now()
			_, err := tinyagent.RunBash(ctx, command)
			if !errors.Is(err, context.Canceled) || time.Since(started) > 2*time.Second {
				t.Fatalf("cancellation took %s: %v", time.Since(started), err)
			}
		})
	}
}
