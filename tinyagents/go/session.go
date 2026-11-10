package tinyagent

import (
	"context"
	"errors"
	"sync"
)

var errSessionBusy = errors.New("A run is already active")
var errSessionClosed = errors.New("Session is closed")

// session is shared by RPC and HTTP. Only successful runs replace its history.
type session struct {
	mu      sync.Mutex
	history []Message
	active  *sessionRun
	closed  bool
}

type sessionRun struct {
	id      string
	ctx     context.Context
	cancel  context.CancelFunc
	done    chan struct{}
	history []Message
}

func (s *session) begin(parent context.Context) (*sessionRun, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, errSessionClosed
	}
	if s.active != nil {
		return nil, errSessionBusy
	}
	ctx, cancel := context.WithCancel(parent)
	run := &sessionRun{id: randomID("r_"), ctx: ctx, cancel: cancel, done: make(chan struct{}), history: cloneHistory(s.history)}
	s.active = run
	return run, nil
}

func (s *session) finish(run *sessionRun, result RunResult, err error) {
	s.mu.Lock()
	if err == nil && run.ctx.Err() == nil && !s.closed {
		s.history = result.History
	}
	s.active = nil
	close(run.done)
	s.mu.Unlock()
	run.cancel()
}

func (s *session) execute(agent *Agent, run *sessionRun, message string, emit func(Event) error) {
	var result RunResult
	err := errors.New("run did not finish")
	defer func() { s.finish(run, result, err) }()
	result, err = agent.Run(run.ctx, message, RunOptions{RunID: run.id, History: run.history, OnEvent: emit})
}

func (s *session) abort() *string {
	s.mu.Lock()
	run := s.active
	s.mu.Unlock()
	if run == nil {
		return nil
	}
	run.cancel()
	return &run.id
}

func (s *session) close() bool {
	s.mu.Lock()
	s.closed = true
	run := s.active
	s.mu.Unlock()
	if run != nil {
		run.cancel()
		<-run.done
	}
	return run != nil
}

type sessionState struct {
	ProtocolVersion int     `json:"protocolVersion"`
	Status          string  `json:"status"`
	RunID           *string `json:"runId"`
	MessageCount    int     `json:"messageCount"`
}

func (s *session) state() sessionState {
	s.mu.Lock()
	defer s.mu.Unlock()
	state := sessionState{ProtocolVersion: ProtocolVersion, Status: "idle", MessageCount: len(s.history)}
	if s.active != nil {
		state.Status, state.RunID = "running", &s.active.id
	}
	return state
}
