package tinyagent

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"unicode/utf8"

	"tinyagent/internal/nativeio"
)

type protocolError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *protocolError) Error() string { return e.Message }

type rpcReply struct {
	Type    string          `json:"type"`
	ID      *string         `json:"id,omitempty"`
	Command string          `json:"command,omitempty"`
	Success *bool           `json:"success,omitempty"`
	Data    json.RawMessage `json:"data,omitempty"`
	Error   *protocolError  `json:"error,omitempty"`
}

// RunRPC serves one in-memory session over UTF-8 JSONL. It closes input on
// cancellation or output failure so a blocked read can stop. EOF cancels a run.
// POSIX files are borrowed through owned, cancellable duplicates. Other inputs
// must unblock when closed; other outputs must return from Write or unblock on
// Close. Callers consume output concurrently. Shutdown joins all I/O and agent
// work; native output gets one second to drain after cancellation.
func RunRPC(parent context.Context, agent *Agent, input io.ReadCloser, output io.Writer) (runErr error) {
	if parent.Err() != nil {
		return nil
	}
	// Keep output alive through cancellation events and cooperative cleanup.
	ioContext, closeIO := context.WithCancel(context.Background())
	defer closeIO()
	inputFile, _ := input.(*os.File)
	outputFile, _ := output.(*os.File)
	files, cleanup, err := nativeio.Open(ioContext, []*os.File{inputFile, outputFile})
	if err != nil {
		return err
	}
	defer func() {
		closeIO()
		runErr = errors.Join(runErr, cleanup())
	}()
	if files[0] != nil {
		input = files[0]
	}
	if files[1] != nil {
		output = files[1]
	}
	ctx, cancel := context.WithCancel(parent)
	inputClosed := make(chan struct{})
	stop := context.AfterFunc(ctx, func() {
		_ = input.Close()
		close(inputClosed)
	})
	writer := newRPCWriter(output, cancel)
	current := &session{}
	defer func() {
		cancel()
		current.close()
		if err := writer.close(); runErr == nil {
			runErr = err
		}
		if !stop() {
			<-inputClosed
		}
	}()
	reader := bufio.NewReader(input)
	for {
		if ctx.Err() != nil {
			return nil
		}
		line, err := readRPCLine(reader)
		if err != nil {
			if errors.Is(err, io.EOF) || ctx.Err() != nil {
				return nil
			}
			var framing *protocolError
			if errors.As(err, &framing) {
				return writer.reply(rpcReply{Type: "protocol_error", Error: framing})
			}
			return err
		}
		if !json.Valid(line) {
			err = writer.commandError(nil, "", "invalid_json", "RPC input is not valid JSON")
		} else {
			var request map[string]json.RawMessage
			if decodeErr := json.Unmarshal(line, &request); decodeErr != nil || request == nil {
				err = writer.commandError(nil, "", "invalid_command", "RPC command must be an object")
			} else {
				err = handleRPC(ctx, agent, &current, writer, request)
			}
		}
		if errors.Is(err, errRPCOutputLimit) {
			current.abort()
		} else if err != nil {
			return err
		}
	}
}

func handleRPC(ctx context.Context, agent *Agent, current **session, writer *rpcWriter,
	request map[string]json.RawMessage) error {
	var id *string
	if raw, exists := request["id"]; exists {
		var value string
		if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &value) != nil {
			return writer.commandError(nil, "", "invalid_command", "command.id must be a string")
		}
		id = &value
	}
	command := "unknown"
	if raw := request["type"]; len(raw) > 0 && raw[0] == '"' {
		_ = json.Unmarshal(raw, &command)
	}
	success := true
	reply := rpcReply{Type: "response", ID: id, Command: command, Success: &success}
	s := *current
	switch command {
	case "prompt":
		var message string
		if json.Unmarshal(request["message"], &message) != nil || message == "" {
			return writer.commandError(id, command, "invalid_command", "prompt.message must be a non-empty string")
		}
		run, err := s.begin(ctx)
		if err != nil {
			return writer.commandError(id, command, "busy", "A run is already active")
		}
		reply.Data, _ = json.Marshal(struct {
			RunID string `json:"runId"`
		}{run.id})
		ack, err := writer.enqueueReply(reply)
		if err != nil {
			s.finish(run, RunResult{}, err)
			return err
		}
		go func() {
			if err := writer.wait(run.ctx, ack); err != nil {
				s.finish(run, RunResult{}, err)
				return
			}
			s.execute(agent, run, message, func(event Event) error {
				data, err := json.Marshal(event)
				if err != nil {
					writer.fail(err)
					return err
				}
				return writer.write(run.ctx, data)
			})
		}()
		return nil
	case "abort":
		runID := s.abort()
		reply.Data, _ = json.Marshal(struct {
			Accepted bool    `json:"accepted"`
			RunID    *string `json:"runId"`
		}{runID != nil, runID})
	case "new_session":
		aborted := s.close()
		*current = &session{}
		reply.Data, _ = json.Marshal(struct {
			AbortedRun bool `json:"abortedRun"`
		}{aborted})
	case "get_state":
		reply.Data, _ = json.Marshal(s.state())
	default:
		return writer.commandError(id, command, "invalid_command", "Unknown command type")
	}
	return writer.reply(reply)
}

func readRPCLine(reader *bufio.Reader) ([]byte, error) {
	var line []byte
	for {
		part, err := reader.ReadSlice('\n')
		part = bytes.TrimSuffix(part, []byte{'\n'})
		if len(line)+len(part) > MaxJSONLLineBytes {
			return nil, &protocolError{Code: "line_too_large", Message: fmt.Sprintf("RPC input exceeds %d UTF-8 bytes", MaxJSONLLineBytes)}
		}
		line = append(line, part...)
		if errors.Is(err, bufio.ErrBufferFull) {
			continue
		}
		if err != nil && (!errors.Is(err, io.EOF) || len(line) == 0) {
			return nil, err
		}
		if !utf8.Valid(line) {
			return nil, &protocolError{Code: "invalid_utf8", Message: "RPC input is not valid UTF-8"}
		}
		return line, nil
	}
}
