package tinyagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
)

type rpcWriteFunc func([]byte) (int, error)

func (write rpcWriteFunc) Write(data []byte) (int, error) { return write(data) }

func TestRPCOutputShortWrites(t *testing.T) {
	var output bytes.Buffer
	output.WriteString("prefix\n")
	writer := newRPCWriter(rpcWriteFunc(func(data []byte) (int, error) {
		return output.Write(data[:min(3, len(data))])
	}), func() { t.Error("successful output cancelled its connection") })
	for _, line := range []string{`{"text":"€\ntext"}`, `{"value":2}`} {
		if err := writer.write(context.Background(), []byte(line)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.close(); err != nil {
		t.Fatal(err)
	}
	if output.String() != "prefix\n"+`{"text":"€\ntext"}`+"\n"+`{"value":2}`+"\n" {
		t.Fatalf("short writes damaged framing: %q", output.String())
	}
}

func TestRPCOutputRejectsInvalidProgress(t *testing.T) {
	for _, written := range []int{-1, 0, 100} {
		ctx, cancel := context.WithCancel(context.Background())
		writer := newRPCWriter(rpcWriteFunc(func([]byte) (int, error) { return written, nil }), cancel)
		want := io.ErrShortWrite
		if written == 0 {
			want = io.ErrNoProgress
		}
		if err := writer.write(ctx, []byte(`{}`)); !errors.Is(err, want) {
			t.Errorf("written=%d error=%v", written, err)
		}
		if err := writer.close(); !errors.Is(err, want) || ctx.Err() == nil {
			t.Errorf("write failure did not close connection: written=%d error=%v cancelled=%v", written, err, ctx.Err())
		}
		cancel()
	}
}

type rpcGatedOutput struct {
	bytes.Buffer
	gate    chan struct{}
	entered chan struct{}
	once    sync.Once
}

func (output *rpcGatedOutput) Write(data []byte) (int, error) {
	output.once.Do(func() { close(output.entered) })
	<-output.gate
	return output.Buffer.Write(data)
}

func TestRPCOutputQueueIncludesWritingFrameAndError(t *testing.T) {
	output := &rpcGatedOutput{gate: make(chan struct{}), entered: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	writer := newRPCWriter(output, cancel)
	line := []byte(`{"data":"` + strings.Repeat("x", 700*1024) + `"}`)
	frame, err := writer.enqueue(line)
	if err != nil {
		t.Fatal(err)
	}
	<-output.entered
	count := 1
	for {
		_, err := writer.enqueue(line)
		if errors.Is(err, errRPCQueueFull) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		count++
		if count > 6 {
			t.Fatal("queue did not apply its byte limit")
		}
	}
	if ctx.Err() == nil {
		t.Error("overflow did not cancel the connection")
	}
	if _, err := writer.enqueue([]byte(`{}`)); !errors.Is(err, errRPCQueueFull) {
		t.Errorf("overflow still admits output: %v", err)
	}
	close(output.gate)
	if err := writer.wait(ctx, frame); err != nil {
		t.Errorf("admitted frame did not drain: %v", err)
	}
	if err := writer.close(); !errors.Is(err, errRPCQueueFull) {
		t.Fatalf("overflow failure was lost: %v", err)
	}
	if output.Len() > MaxQueuedOutputBytes || output.Len() != count*(len(line)+1)+len(rpcQueueError) {
		t.Fatalf("queue bytes=%d admitted=%d", output.Len(), count)
	}
	lines := bytes.Split(bytes.TrimSuffix(output.Bytes(), []byte{'\n'}), []byte{'\n'})
	if len(lines) != count+1 || !bytes.Equal(append(lines[len(lines)-1], '\n'), rpcQueueError) {
		t.Fatal("overflow did not emit exactly one final error")
	}
	for _, line := range lines {
		if !json.Valid(line) {
			t.Fatal("queue interleaved JSONL frames")
		}
	}
}

func TestRPCOutputLimitPreservesConnection(t *testing.T) {
	var output bytes.Buffer
	writer := newRPCWriter(&output, func() { t.Error("line limit closed the connection") })
	if err := writer.write(context.Background(), bytes.Repeat([]byte{'x'}, MaxJSONLLineBytes+1)); !errors.Is(err, errRPCOutputLimit) {
		t.Fatalf("oversized output error=%v", err)
	}
	if err := writer.write(context.Background(), []byte(`{"next":true}`)); err != nil {
		t.Fatal(err)
	}
	if err := writer.close(); err != nil {
		t.Fatal(err)
	}
	lines := bytes.Split(bytes.TrimSpace(output.Bytes()), []byte{'\n'})
	var first rpcReply
	if len(lines) != 2 || json.Unmarshal(lines[0], &first) != nil || first.Error == nil || first.Error.Code != "output_too_large" {
		t.Fatalf("missing bounded output limit response: %s", output.Bytes())
	}
}
