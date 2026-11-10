package tinyagent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
	"time"
)

var errRPCOutputLimit = errors.New("RPC output exceeds its byte limit")
var errRPCOutputDrain = errors.New("RPC output did not drain after cancellation")
var errRPCQueueFull = errors.New("RPC output queue is full")

const rpcDrainTimeout = time.Second

var rpcQueueError = []byte(`{"type":"protocol_error","error":{"code":"output_queue_full","message":"RPC output queue is full"}}` + "\n")

type rpcFrame struct {
	line   []byte
	done   chan error
	result error
}

// One worker owns output. Replies queue without blocking command input; prompt
// acknowledgements and events wait for delivery before agent work can proceed.
type rpcWriter struct {
	mu          sync.Mutex
	ready       *sync.Cond
	queue       []*rpcFrame
	queuedBytes int // Includes the frame currently being written and every LF.
	closing     bool
	failed      bool
	err         error
	done        chan struct{}
	output      io.Writer
	cancel      context.CancelFunc
	interrupt   func()
}

func newRPCWriter(output io.Writer, cancel context.CancelFunc) *rpcWriter {
	writer := &rpcWriter{output: output, cancel: cancel, done: make(chan struct{})}
	writer.interrupt = func() {
		if closer, ok := output.(io.Closer); ok {
			_ = closer.Close()
		}
	}
	writer.ready = sync.NewCond(&writer.mu)
	go writer.work()
	return writer
}

func (writer *rpcWriter) enqueue(data []byte) (*rpcFrame, error) {
	var result error
	if len(data) > MaxJSONLLineBytes {
		result = errRPCOutputLimit
		data, _ = json.Marshal(rpcReply{Type: "protocol_error", Error: &protocolError{
			Code: "output_too_large", Message: fmt.Sprintf("RPC output exceeds %d UTF-8 bytes", MaxJSONLLineBytes),
		}})
	}
	frame := &rpcFrame{line: append(append([]byte(nil), data...), '\n'), done: make(chan error, 1), result: result}
	writer.mu.Lock()
	if writer.closing || writer.failed {
		err := writer.err
		if err == nil {
			err = io.ErrClosedPipe
		}
		writer.mu.Unlock()
		return nil, err
	}
	// Reserve space for exactly one bounded overflow error.
	overflow := writer.queuedBytes+len(frame.line) > MaxQueuedOutputBytes-len(rpcQueueError)
	if overflow {
		frame.line, frame.result = rpcQueueError, errRPCQueueFull
		writer.closing, writer.err = true, errRPCQueueFull
	}
	writer.queue = append(writer.queue, frame)
	writer.queuedBytes += len(frame.line)
	writer.ready.Signal()
	writer.mu.Unlock()
	if overflow {
		writer.cancel()
		return nil, errRPCQueueFull
	}
	return frame, result
}

func (writer *rpcWriter) enqueueReply(reply rpcReply) (*rpcFrame, error) {
	data, err := json.Marshal(reply)
	if err != nil {
		return nil, err
	}
	return writer.enqueue(data)
}

func (writer *rpcWriter) reply(reply rpcReply) error {
	_, err := writer.enqueueReply(reply)
	return err
}

func (writer *rpcWriter) commandError(id *string, command, code, message string) error {
	reply := rpcReply{Type: "protocol_error", Error: &protocolError{Code: code, Message: message}}
	if id != nil {
		failed := false
		reply.Type, reply.ID, reply.Command, reply.Success = "response", id, command, &failed
	}
	return writer.reply(reply)
}

func (writer *rpcWriter) write(ctx context.Context, data []byte) error {
	frame, err := writer.enqueue(data)
	if frame == nil {
		return err
	}
	return writer.wait(ctx, frame)
}

func (writer *rpcWriter) wait(ctx context.Context, frame *rpcFrame) error {
	select {
	case err := <-frame.done:
		return err
	case <-ctx.Done():
	}
	// Cancellation still permits paired tool/end events to drain. Start this
	// deadline only for a pending write, never for cooperative tool cleanup.
	timer := time.NewTimer(rpcDrainTimeout)
	defer timer.Stop()
	select {
	case err := <-frame.done:
		return err
	case <-timer.C:
		select {
		case err := <-frame.done:
			return err
		default:
			writer.fail(errRPCOutputDrain)
			return <-frame.done
		}
	}
}

func (writer *rpcWriter) fail(err error) {
	writer.mu.Lock()
	if writer.failed {
		writer.mu.Unlock()
		return
	}
	writer.failed = true
	if writer.err == nil {
		writer.err = err
	}
	writer.ready.Broadcast()
	writer.mu.Unlock()
	writer.cancel()
	writer.interrupt()
}

func (writer *rpcWriter) work() {
	defer close(writer.done)
	for {
		writer.mu.Lock()
		for len(writer.queue) == 0 && !writer.closing && !writer.failed {
			writer.ready.Wait()
		}
		if writer.failed {
			for _, frame := range writer.queue {
				frame.done <- writer.err
			}
			writer.queue, writer.queuedBytes = nil, 0
			writer.mu.Unlock()
			return
		}
		if len(writer.queue) == 0 {
			writer.mu.Unlock()
			return
		}
		frame := writer.queue[0]
		writer.queue[0] = nil
		writer.queue = writer.queue[1:]
		writer.mu.Unlock()
		err := writeRPCLine(writer.output, frame.line)
		if err != nil {
			writer.fail(err)
		}
		writer.mu.Lock()
		writer.queuedBytes -= len(frame.line)
		if writer.failed {
			err = writer.err
		} else {
			err = frame.result
		}
		writer.mu.Unlock()
		frame.done <- err
	}
}

func writeRPCLine(output io.Writer, line []byte) error {
	for len(line) != 0 {
		written, err := output.Write(line)
		if written < 0 || written > len(line) {
			return io.ErrShortWrite
		}
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrNoProgress
		}
		line = line[written:]
	}
	return nil
}

func (writer *rpcWriter) close() error {
	writer.mu.Lock()
	writer.closing = true
	writer.ready.Broadcast()
	writer.mu.Unlock()
	timer := time.NewTimer(rpcDrainTimeout)
	defer timer.Stop()
	select {
	case <-writer.done:
	case <-timer.C:
		select {
		case <-writer.done:
		default:
			writer.fail(errRPCOutputDrain)
			<-writer.done
		}
	}
	writer.mu.Lock()
	defer writer.mu.Unlock()
	return writer.err
}
