//go:build !windows

package cli

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"tinyagent"
	"tinyagent/internal/nativeio"
)

type consoleProvider func(context.Context, tinyagent.ProviderRequest, func(string) error) (tinyagent.ProviderTurn, error)

func (provider consoleProvider) Stream(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
	return provider(ctx, request, emit)
}

func TestInteractiveCancellationReleasesNativeOutput(t *testing.T) {
	for _, diagnostic := range []bool{false, true} {
		name := "stdout"
		if diagnostic {
			name = "stderr"
		}
		t.Run(name, func(t *testing.T) {
			reader, writer, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer reader.Close()
			defer writer.Close()
			entered := make(chan struct{})
			closed := make(chan struct{})
			provider := consoleProvider(func(ctx context.Context, request tinyagent.ProviderRequest, emit func(string) error) (tinyagent.ProviderTurn, error) {
				close(entered)
				defer close(closed)
				text := strings.Repeat("x", 512*1024)
				if diagnostic {
					return tinyagent.ProviderTurn{}, errors.New(text)
				}
				if err := emit(text); err != nil {
					return tinyagent.ProviderTurn{}, err
				}
				return tinyagent.ProviderTurn{Text: text, Assistant: tinyagent.Message{Role: "assistant", Text: text}}, nil
			})
			agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{Model: "fixture", Provider: provider})
			if err != nil {
				t.Fatal(err)
			}
			var buffer bytes.Buffer
			var output, diagnostics io.Writer = writer, &buffer
			if diagnostic {
				output, diagnostics = &buffer, writer
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() {
				done <- interactive(ctx, agent, io.NopCloser(strings.NewReader("go\n")), output, diagnostics)
			}()
			select {
			case <-entered:
			case <-time.After(3 * time.Second):
				reader.Close()
				<-done
				t.Fatal("provider did not start")
			}
			select {
			case err := <-done:
				t.Fatalf("native output did not block: %v", err)
			case <-time.After(30 * time.Millisecond):
			}
			cancel()
			select {
			case err := <-done:
				if err != nil {
					t.Errorf("interrupted CLI returned %v", err)
				}
			case <-time.After(2 * time.Second):
				// Release the failed implementation before reporting the regression.
				reader.Close()
				<-done
				t.Fatal("cancellation left the CLI blocked in native output")
			}
			select {
			case <-closed:
			default:
				t.Error("CLI returned before provider cleanup")
			}
			if _, err := writer.Stat(); err != nil {
				t.Errorf("CLI closed its caller's output: %v", err)
			}
		})
	}
}

func consoleFlags(file *os.File, command uintptr) (int, error) {
	raw, err := file.SyscallConn()
	if err != nil {
		return 0, err
	}
	var flags uintptr
	var errno syscall.Errno
	err = raw.Control(func(fd uintptr) {
		flags, _, errno = syscall.Syscall(syscall.SYS_FCNTL, fd, command, 0)
	})
	if errno != 0 {
		return 0, errno
	}
	return int(flags), err
}

func consoleDirectory(t *testing.T) string {
	t.Helper()
	directory, err := os.MkdirTemp(".", ".test-console-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(directory) })
	directory, err = filepath.Abs(directory)
	if err != nil {
		t.Fatal(err)
	}
	return directory
}

func TestConsoleRestoresSharedFileModes(t *testing.T) {
	for _, nonblocking := range []bool{false, true} {
		reader, writer, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		defer reader.Close()
		defer writer.Close()
		raw, err := writer.SyscallConn()
		if err != nil {
			t.Fatal(err)
		}
		var flagErr error
		if err := raw.Control(func(fd uintptr) { flagErr = syscall.SetNonblock(int(fd), nonblocking) }); err != nil || flagErr != nil {
			t.Fatalf("set initial mode: %v %v", err, flagErr)
		}
		before, err := consoleFlags(writer, syscall.F_GETFD)
		if err != nil {
			t.Fatal(err)
		}
		files, cleanup, err := nativeio.Open(context.Background(), []*os.File{nil, writer, writer})
		if err != nil {
			t.Fatal(err)
		}
		for _, file := range files[1:] {
			flags, err := consoleFlags(file, syscall.F_GETFL)
			if err != nil || flags&syscall.O_NONBLOCK == 0 {
				t.Errorf("duplicate is not nonblocking: flags=%d error=%v", flags, err)
			}
			flags, err = consoleFlags(file, syscall.F_GETFD)
			if err != nil || flags&syscall.FD_CLOEXEC == 0 {
				t.Errorf("duplicate can leak into child processes: flags=%d error=%v", flags, err)
			}
		}
		if err := cleanup(); err != nil {
			t.Fatal(err)
		}
		flags, err := consoleFlags(writer, syscall.F_GETFL)
		if err != nil || (flags&syscall.O_NONBLOCK != 0) != nonblocking {
			t.Fatalf("shared file mode not restored: flags=%d error=%v", flags, err)
		}
		if after, err := consoleFlags(writer, syscall.F_GETFD); err != nil || after != before {
			t.Fatalf("source descriptor flags changed: before=%d after=%d error=%v", before, after, err)
		}
		for _, file := range files[1:] {
			if _, err := file.Stat(); !errors.Is(err, os.ErrClosed) {
				t.Fatalf("duplicate remains open: %v", err)
			}
		}
	}
}

func TestInteractiveNativeFilesRetainOwnership(t *testing.T) {
	directory := consoleDirectory(t)
	var files []*os.File
	for _, name := range []string{"input", "output", "diagnostics"} {
		file, err := os.Create(filepath.Join(directory, name))
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		files = append(files, file)
	}
	if _, err := files[0].WriteString("first\nfail\nsecond\n"); err != nil {
		t.Fatal(err)
	}
	if _, err := files[0].Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	provider := &cliProvider{}
	agent, err := tinyagent.NewAgent(tinyagent.AgentConfig{Model: "fixture", Provider: provider})
	if err != nil {
		t.Fatal(err)
	}
	if err := interactive(context.Background(), agent, files[0], files[1], files[2]); err != nil {
		t.Fatal(err)
	}
	if provider.requests != 3 {
		t.Fatalf("requests=%d", provider.requests)
	}
	for _, file := range files {
		if _, err := file.Stat(); err != nil {
			t.Errorf("borrowed file closed: %v", err)
		}
	}
	text, err := os.ReadFile(filepath.Join(directory, "output"))
	if err != nil || !strings.Contains(string(text), "> first\n") || !strings.Contains(string(text), "> second\n") {
		t.Fatalf("output=%s error=%v", text, err)
	}
	diagnostics, err := os.ReadFile(filepath.Join(directory, "diagnostics"))
	if err != nil || string(diagnostics) != "error: fixture failure\n" {
		t.Fatalf("diagnostics=%s error=%v", diagnostics, err)
	}
}
