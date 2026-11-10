package tinyagent

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

type httpHandler struct {
	agent    *Agent
	token    string
	mu       sync.Mutex
	sessions map[string]*session
	closing  bool
}

func (handler *httpHandler) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	authorization := request.Header.Values("Authorization")
	if handler.token != "" && (len(authorization) != 1 || subtle.ConstantTimeCompare(
		[]byte(authorization[0]), []byte("Bearer "+handler.token)) != 1) {
		writer.Header().Set("WWW-Authenticate", "Bearer")
		writeHTTPError(writer, http.StatusUnauthorized, "unauthorized", "Missing or invalid bearer token")
		return
	}
	handler.mu.Lock()
	closing := handler.closing
	handler.mu.Unlock()
	if closing {
		writeHTTPError(writer, http.StatusServiceUnavailable, "server_closed", "Server is shutting down")
		return
	}
	if request.Method == http.MethodGet && request.URL.Path == "/health" {
		data, _ := json.Marshal(struct {
			Status          string `json:"status"`
			ProtocolVersion int    `json:"protocolVersion"`
		}{"ok", ProtocolVersion})
		writeHTTPJSON(writer, http.StatusOK, data)
		return
	}
	if request.Method == http.MethodPost && request.URL.Path == "/sessions" {
		if _, ok := readHTTPObject(writer, request, true); !ok {
			return
		}
		id := randomID("s_")
		handler.mu.Lock()
		if handler.closing {
			handler.mu.Unlock()
			writeHTTPError(writer, http.StatusServiceUnavailable, "server_closed", "Server is shutting down")
			return
		}
		handler.sessions[id] = &session{}
		handler.mu.Unlock()
		data, _ := json.Marshal(struct {
			SessionID string `json:"sessionId"`
		}{id})
		writeHTTPJSON(writer, http.StatusCreated, data)
		return
	}
	parts := strings.Split(strings.TrimPrefix(request.URL.Path, "/"), "/")
	if len(parts) < 2 || len(parts) > 3 || parts[0] != "sessions" {
		writeHTTPError(writer, http.StatusNotFound, "not_found", "Unknown route")
		return
	}
	handler.mu.Lock()
	current := handler.sessions[parts[1]]
	handler.mu.Unlock()
	if current == nil {
		writeHTTPError(writer, http.StatusNotFound, "session_not_found", "Unknown session")
		return
	}
	if request.Method == http.MethodDelete && len(parts) == 2 {
		current.close()
		handler.mu.Lock()
		delete(handler.sessions, parts[1])
		handler.mu.Unlock()
		writer.WriteHeader(http.StatusNoContent)
		return
	}
	if request.Method == http.MethodPost && len(parts) == 3 {
		switch parts[2] {
		case "prompts":
			handler.prompt(writer, request, current)
			return
		case "cancel":
			if _, ok := readHTTPObject(writer, request, true); !ok {
				return
			}
			runID := current.abort()
			data, _ := json.Marshal(struct {
				Accepted bool    `json:"accepted"`
				RunID    *string `json:"runId"`
			}{runID != nil, runID})
			writeHTTPJSON(writer, http.StatusOK, data)
			return
		}
	}
	writeHTTPError(writer, http.StatusNotFound, "not_found", "Unknown route")
}

func (handler *httpHandler) prompt(writer http.ResponseWriter, request *http.Request, current *session) {
	object, ok := readHTTPObject(writer, request, false)
	if !ok {
		return
	}
	var message string
	if json.Unmarshal(object["message"], &message) != nil || message == "" {
		writeHTTPError(writer, http.StatusBadRequest, "invalid_request", "message must be a non-empty string")
		return
	}
	run, err := current.begin(request.Context())
	if err != nil {
		if errors.Is(err, errSessionBusy) {
			writeHTTPError(writer, http.StatusConflict, "session_busy", "The session already has an active run")
		} else {
			writeHTTPError(writer, http.StatusNotFound, "session_not_found", "Unknown session")
		}
		return
	}
	writer.Header().Set("Content-Type", "application/jsonl; charset=utf-8")
	writer.Header().Set("Cache-Control", "no-store")
	// Close delimits a prompt response. net/http otherwise writes a final chunk
	// after this handler returns, too late to observe its failure before commit.
	writer.Header().Set("Connection", "close")
	writer.Header().Set("Transfer-Encoding", "identity")
	current.execute(handler.agent, run, message, func(event Event) error {
		data, err := json.Marshal(event)
		if err != nil {
			return err
		}
		oversized := len(data) > MaxJSONLLineBytes
		if oversized {
			run.cancel()
			data, _ = json.Marshal(Event{Type: "agent_end", RunID: event.RunID, Sequence: event.Sequence,
				Status: "failed", Error: &RunError{Code: "output_too_large",
					Message: fmt.Sprintf("HTTP output exceeds %d UTF-8 bytes", MaxJSONLLineBytes)}})
		}
		if err := writeHTTPLine(run.ctx, writer, append(data, '\n')); err != nil {
			return err
		}
		if oversized {
			return errors.New("HTTP output exceeds its byte limit")
		}
		return nil
	})
}

func writeHTTPLine(ctx context.Context, writer http.ResponseWriter, line []byte) error {
	controller := http.NewResponseController(writer)
	// Bound a pending write after cancellation, while leaving healthy output
	// available for paired tool/end events after slow cooperative cleanup.
	deadlineSet := make(chan struct{})
	stop := context.AfterFunc(ctx, func() {
		_ = controller.SetWriteDeadline(time.Now().Add(time.Second))
		close(deadlineSet)
	})
	defer func() {
		if !stop() {
			<-deadlineSet
		}
		_ = controller.SetWriteDeadline(time.Time{})
	}()
	written, err := writer.Write(line)
	if err != nil {
		return err
	}
	if written != len(line) {
		return io.ErrShortWrite
	}
	return controller.Flush()
}

func (handler *httpHandler) shutdown() {
	handler.mu.Lock()
	handler.closing = true
	sessions := make([]*session, 0, len(handler.sessions))
	for _, current := range handler.sessions {
		sessions = append(sessions, current)
	}
	handler.mu.Unlock()
	for _, current := range sessions {
		current.abort()
	}
	for _, current := range sessions {
		current.close()
	}
	handler.mu.Lock()
	clear(handler.sessions)
	handler.mu.Unlock()
}

func readHTTPObject(writer http.ResponseWriter, request *http.Request, optional bool) (map[string]json.RawMessage, bool) {
	if request.ContentLength > MaxHTTPBodyBytes {
		writeHTTPError(writer, http.StatusRequestEntityTooLarge, "body_too_large", "Request body exceeds its byte limit")
		return nil, false
	}
	body, err := io.ReadAll(io.LimitReader(request.Body, MaxHTTPBodyBytes+1))
	if err != nil {
		writeHTTPError(writer, http.StatusBadRequest, "invalid_request", "Could not read request body")
		return nil, false
	}
	if len(body) > MaxHTTPBodyBytes {
		writeHTTPError(writer, http.StatusRequestEntityTooLarge, "body_too_large", "Request body exceeds its byte limit")
		return nil, false
	}
	if optional && len(body) == 0 {
		return nil, true
	}
	contentTypes := request.Header.Values("Content-Type")
	mediaType, parameters, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
	if len(contentTypes) != 1 || err != nil || mediaType != "application/json" || (parameters["charset"] != "" && !strings.EqualFold(parameters["charset"], "utf-8")) {
		writeHTTPError(writer, http.StatusUnsupportedMediaType, "unsupported_media_type", "Expected application/json with UTF-8 encoding")
		return nil, false
	}
	if !utf8.Valid(body) {
		writeHTTPError(writer, http.StatusBadRequest, "invalid_utf8", "Request body is not valid UTF-8")
		return nil, false
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(bytes.TrimSpace(body), &object) != nil || object == nil {
		writeHTTPError(writer, http.StatusBadRequest, "invalid_request", "Request body must be a JSON object")
		return nil, false
	}
	return object, true
}

func writeHTTPError(writer http.ResponseWriter, status int, code, message string) {
	data, _ := json.Marshal(struct {
		Error RunError `json:"error"`
	}{RunError{Code: code, Message: message, Retryable: status == http.StatusConflict || status >= 500}})
	writeHTTPJSON(writer, status, data)
}

func writeHTTPJSON(writer http.ResponseWriter, status int, data []byte) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.Header().Set("Cache-Control", "no-store")
	writer.WriteHeader(status)
	_, _ = writer.Write(append(data, '\n'))
}
