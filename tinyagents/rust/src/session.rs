use crate::{
    Agent, CancellationToken, Event, Message, Provider, Result, RunOptions, RunResult,
    PROTOCOL_VERSION,
};
use serde::Serialize;
use std::{
    future::Future,
    sync::{Arc, Mutex},
};
use tokio::sync::watch;
use uuid::Uuid;

#[derive(Default)]
pub(crate) struct Session {
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    history: Vec<Message>,
    active: Option<Active>,
    closed: bool,
}

struct Active {
    id: String,
    cancellation: CancellationToken,
    done: watch::Receiver<bool>,
}

#[derive(Debug)]
pub(crate) enum StartError {
    Busy,
    Closed,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SessionState {
    protocol_version: u32,
    status: &'static str,
    run_id: Option<String>,
    message_count: usize,
}

pub(crate) struct SessionRun {
    session: Arc<Session>,
    pub id: String,
    pub cancellation: CancellationToken,
    history: Vec<Message>,
    commit: Option<Vec<Message>>,
    done: watch::Sender<bool>,
}

impl Session {
    pub fn begin(
        self: &Arc<Self>,
        parent: &CancellationToken,
    ) -> std::result::Result<SessionRun, StartError> {
        let mut state = self.state.lock().unwrap();
        if state.closed {
            return Err(StartError::Closed);
        }
        if state.active.is_some() {
            return Err(StartError::Busy);
        }
        let id = format!("r_{}", Uuid::new_v4().simple());
        let cancellation = parent.child_token();
        let (done, receiver) = watch::channel(false);
        state.active = Some(Active {
            id: id.clone(),
            cancellation: cancellation.clone(),
            done: receiver,
        });
        Ok(SessionRun {
            session: self.clone(),
            id,
            cancellation,
            history: state.history.clone(),
            commit: None,
            done,
        })
    }

    pub fn abort(&self) -> Option<String> {
        let state = self.state.lock().unwrap();
        state.active.as_ref().map(|run| {
            run.cancellation.cancel();
            run.id.clone()
        })
    }

    pub async fn close(&self) -> bool {
        let active = {
            let mut state = self.state.lock().unwrap();
            state.closed = true;
            state.active.as_ref().map(|run| {
                run.cancellation.cancel();
                run.done.clone()
            })
        };
        if let Some(mut done) = active {
            let _ = done.wait_for(|done| *done).await;
            true
        } else {
            false
        }
    }

    pub fn state(&self) -> SessionState {
        let state = self.state.lock().unwrap();
        SessionState {
            protocol_version: PROTOCOL_VERSION,
            status: if state.active.is_some() {
                "running"
            } else {
                "idle"
            },
            run_id: state.active.as_ref().map(|run| run.id.clone()),
            message_count: state.history.len(),
        }
    }
}

impl SessionRun {
    pub async fn execute<P, F, Fut>(
        mut self,
        agent: &Agent<P>,
        prompt: &str,
        callback: F,
    ) -> Result<RunResult>
    where
        P: Provider,
        F: FnMut(Event) -> Fut + Send,
        Fut: Future<Output = Result<()>> + Send,
    {
        let result = agent
            .run_with_events(
                prompt,
                RunOptions {
                    run_id: Some(self.id.clone()),
                    history: std::mem::take(&mut self.history),
                    cancellation: self.cancellation.clone(),
                },
                callback,
            )
            .await;
        if let Ok(result) = &result {
            self.commit = Some(result.history.clone());
        }
        result
    }
}

// Drop also handles task cancellation and panics without leaving a busy session.
impl Drop for SessionRun {
    fn drop(&mut self) {
        let mut state = self.session.state.lock().unwrap();
        if !state.closed && !self.cancellation.is_cancelled() {
            if let Some(history) = self.commit.take() {
                state.history = history;
            }
        }
        state.active = None;
        self.done.send_replace(true);
        self.cancellation.cancel();
    }
}
