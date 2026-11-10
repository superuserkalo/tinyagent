use super::protocol_error;
use crate::{CancellationToken, Error, Result, MAX_JSONL_LINE_BYTES, MAX_QUEUED_OUTPUT_BYTES};
use serde::Serialize;
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncWrite, AsyncWriteExt},
    sync::{oneshot, Notify},
    time::timeout,
};
use tokio_util::task::AbortOnDropHandle;

const DRAIN_TIMEOUT: Duration = Duration::from_secs(1);
const DRAIN_ERROR: &str = "RPC output did not drain after cancellation";
const QUEUE_ERROR: &[u8] = b"{\"type\":\"protocol_error\",\"error\":{\"code\":\"output_queue_full\",\"message\":\"RPC output queue is full\"}}\n";

pub(super) struct Receipt {
    done: oneshot::Receiver<()>,
    pub oversized: bool,
}

struct Frame {
    line: Vec<u8>,
    done: oneshot::Sender<()>,
}

#[derive(Default)]
struct State {
    queue: VecDeque<Frame>,
    bytes: usize,
    closing: bool,
    error: Option<String>,
}

// A single worker owns output. Command replies enqueue without awaiting I/O;
// acknowledgements and events wait for a receipt before agent work proceeds.
pub(super) struct Writer {
    state: Mutex<State>,
    ready: Notify,
    failed: CancellationToken,
    cancellation: CancellationToken,
}

impl Writer {
    pub fn start<W>(
        output: W,
        cancellation: CancellationToken,
    ) -> (Arc<Self>, AbortOnDropHandle<()>)
    where
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let writer = Arc::new(Self {
            state: Mutex::new(State::default()),
            ready: Notify::new(),
            failed: CancellationToken::new(),
            cancellation,
        });
        let worker = writer.clone();
        let task = AbortOnDropHandle::new(tokio::spawn(async move {
            let _cancel_on_exit = worker.cancellation.clone().drop_guard();
            worker.work(output).await;
        }));
        (writer, task)
    }

    pub fn enqueue<T: Serialize>(&self, value: T) -> Result<Receipt> {
        let mut line =
            serde_json::to_vec(&value).map_err(|error| Error::Consumer(error.to_string()))?;
        let oversized = line.len() > MAX_JSONL_LINE_BYTES;
        if oversized {
            line = serde_json::to_vec(&protocol_error(
                "output_too_large",
                &format!("RPC output exceeds {MAX_JSONL_LINE_BYTES} UTF-8 bytes"),
            ))
            .unwrap();
        }
        line.push(b'\n');
        let (done, receipt) = oneshot::channel();
        let mut state = self.state.lock().unwrap();
        if state.closing || self.failed.is_cancelled() {
            return Err(Error::Consumer(
                state
                    .error
                    .clone()
                    .unwrap_or_else(|| "RPC output is closed".into()),
            ));
        }
        // The budget includes the pending write and reserves its final error.
        let overflow = state.bytes + line.len() > MAX_QUEUED_OUTPUT_BYTES - QUEUE_ERROR.len();
        if overflow {
            line = QUEUE_ERROR.to_vec();
            state.closing = true;
            state.error = Some("RPC output queue is full".into());
        }
        state.bytes += line.len();
        state.queue.push_back(Frame { line, done });
        drop(state);
        self.ready.notify_one();
        if overflow {
            self.cancellation.cancel();
            return Err(self.error());
        }
        Ok(Receipt {
            done: receipt,
            oversized,
        })
    }

    pub async fn wait(&self, mut receipt: Receipt, cancellation: &CancellationToken) -> Result<()> {
        let delivered = async {
            tokio::select! {
                biased;
                _ = self.failed.cancelled() => Err(self.error()),
                result = &mut receipt.done => result.map_err(|_| self.error()),
            }
        };
        tokio::pin!(delivered);
        tokio::select! {
            result = &mut delivered => result?,
            _ = cancellation.cancelled() => match timeout(DRAIN_TIMEOUT, &mut delivered).await {
                Ok(result) => result?,
                Err(_) => {
                    self.fail(DRAIN_ERROR.into());
                    return Err(self.error());
                }
            },
        }
        if receipt.oversized {
            return Err(Error::Consumer("RPC output exceeds its byte limit".into()));
        }
        Ok(())
    }

    pub async fn close(&self, mut task: AbortOnDropHandle<()>) -> Result<()> {
        self.state.lock().unwrap().closing = true;
        self.ready.notify_one();
        let joined = match timeout(DRAIN_TIMEOUT, &mut task).await {
            Ok(result) => result,
            Err(_) => {
                self.fail(DRAIN_ERROR.into());
                task.await
            }
        };
        joined.map_err(|error| Error::Consumer(error.to_string()))?;
        match self.state.lock().unwrap().error.clone() {
            Some(error) => Err(Error::Consumer(error)),
            None => Ok(()),
        }
    }

    fn error(&self) -> Error {
        Error::Consumer(
            self.state
                .lock()
                .unwrap()
                .error
                .clone()
                .unwrap_or_else(|| "RPC output is closed".into()),
        )
    }

    fn fail(&self, error: String) {
        let mut state = self.state.lock().unwrap();
        state.error.get_or_insert(error);
        state.queue.clear();
        drop(state);
        self.failed.cancel();
        self.cancellation.cancel();
    }

    async fn work<W: AsyncWrite + Unpin>(&self, mut output: W) {
        loop {
            let ready = self.ready.notified();
            let frame = {
                let mut state = self.state.lock().unwrap();
                if self.failed.is_cancelled() || (state.closing && state.queue.is_empty()) {
                    return;
                }
                state.queue.pop_front()
            };
            let Some(frame) = frame else {
                tokio::select! {
                    _ = self.failed.cancelled() => return,
                    _ = ready => continue,
                }
            };
            let written = tokio::select! {
                biased;
                _ = self.failed.cancelled() => return,
                result = async {
                    output.write_all(&frame.line).await?;
                    output.flush().await
                } => result,
            };
            if let Err(error) = written {
                self.fail(error.to_string());
                return;
            }
            self.state.lock().unwrap().bytes -= frame.line.len();
            let _ = frame.done.send(());
        }
    }
}
