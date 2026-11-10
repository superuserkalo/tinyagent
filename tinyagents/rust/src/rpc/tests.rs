use super::{output::Writer, Session};
use crate::{
    Agent, AgentConfig, CancellationToken, Error, Provider, ProviderEvent, ProviderRequest,
    ProviderStream, ProviderTurn, Result, MAX_JSONL_LINE_BYTES, MAX_QUEUED_OUTPUT_BYTES,
};
use futures_util::{stream, task::AtomicWaker};
use serde_json::{json, Value};
use std::{
    io,
    pin::Pin,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWrite},
    sync::Notify,
    time::timeout,
};

#[tokio::test]
async fn queue_budget_includes_pending_utf8_bytes_and_a_single_overflow_error() {
    let cancellation = CancellationToken::new();
    let (output, mut reader) = tokio::io::duplex(8192);
    let (writer, task) = Writer::start(output, cancellation.clone());
    let value = json!({"text":"é".repeat(256 * 1024)});
    let frame_bytes = serde_json::to_vec(&value).unwrap().len() + 1;
    writer.enqueue(&value).unwrap();
    tokio::task::yield_now().await; // The first frame is now the pending write.
    let mut accepted = 1;
    while writer.enqueue(&value).is_ok() {
        accepted += 1;
        assert!(accepted < 10, "output queue is not bounded");
    }
    assert!(cancellation.is_cancelled());
    assert!(writer.enqueue(json!({"type":"get_state"})).is_err());
    let mut bytes = Vec::new();
    let (result, read) = timeout(Duration::from_secs(3), async {
        tokio::join!(writer.close(task), reader.read_to_end(&mut bytes))
    })
    .await
    .unwrap();
    assert!(matches!(result, Err(Error::Consumer(ref text)) if text == "RPC output queue is full"));
    read.unwrap();
    assert!(bytes.len() <= MAX_QUEUED_OUTPUT_BYTES);
    let lines = bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>();
    assert_eq!(lines.len(), accepted + 1);
    for line in &lines[..accepted] {
        assert!(line.len() <= MAX_JSONL_LINE_BYTES);
        assert_eq!(serde_json::from_slice::<Value>(line).unwrap(), value);
    }
    let error = serde_json::from_slice::<Value>(lines[accepted]).unwrap();
    assert_eq!(error["error"]["code"], "output_queue_full");
    assert_eq!(
        bytes.len(),
        accepted * frame_bytes + lines[accepted].len() + 1
    );
    assert!(bytes.len() + frame_bytes > MAX_QUEUED_OUTPUT_BYTES);
}

struct CompleteProvider;

impl Provider for CompleteProvider {
    async fn stream(&self, _: ProviderRequest) -> Result<ProviderStream> {
        Ok(Box::pin(stream::iter([Ok(ProviderEvent::Turn(
            ProviderTurn {
                text: "done".into(),
                tool_calls: vec![],
                replay: vec![],
                output: vec![],
            },
        ))])))
    }
}

#[derive(Default)]
struct FlushGate {
    reached: Notify,
    released: AtomicBool,
    waker: AtomicWaker,
    dropped: Notify,
}

struct HeldTerminal {
    gate: Arc<FlushGate>,
    fail: bool,
    terminal: bool,
}

impl AsyncWrite for HeldTerminal {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        let frame: Value = serde_json::from_slice(bytes).unwrap();
        self.terminal = frame["type"] == "agent_end";
        Poll::Ready(Ok(bytes.len()))
    }

    fn poll_flush(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        if self.terminal {
            self.gate.waker.register(context.waker());
            if !self.gate.released.load(Ordering::SeqCst) {
                self.gate.reached.notify_one();
                return Poll::Pending;
            }
            if self.fail {
                return Poll::Ready(Err(io::Error::other("terminal flush failed")));
            }
        }
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

impl Drop for HeldTerminal {
    fn drop(&mut self) {
        self.gate.dropped.notify_one();
    }
}

#[tokio::test]
async fn terminal_flush_controls_history_commit_and_cancelled_runs_roll_back() {
    for (fail, abort) in [(false, false), (true, false), (false, true)] {
        let gate = Arc::new(FlushGate::default());
        let cancellation = CancellationToken::new();
        let (writer, output_task) = Writer::start(
            HeldTerminal {
                gate: gate.clone(),
                fail,
                terminal: false,
            },
            cancellation.clone(),
        );
        let session = Arc::new(Session::default());
        let run = session.begin(&cancellation).unwrap();
        let token = run.cancellation.clone();
        let output = writer.clone();
        let task = tokio::spawn(async move {
            let agent = Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                provider: CompleteProvider,
                tools: vec![],
            })
            .unwrap();
            run.execute(&agent, "finish", move |event| {
                let output = output.clone();
                let token = token.clone();
                async move { output.wait(output.enqueue(event)?, &token).await }
            })
            .await
        });
        timeout(Duration::from_secs(1), gate.reached.notified())
            .await
            .unwrap();
        let state = serde_json::to_value(session.state()).unwrap();
        assert_eq!(state["status"], "running");
        assert_eq!(state["messageCount"], 0);
        if abort {
            session.abort();
        }
        gate.released.store(true, Ordering::SeqCst);
        gate.waker.wake();
        let result = timeout(Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(result.is_ok(), !fail && !abort);
        let state = serde_json::to_value(session.state()).unwrap();
        assert_eq!(state["status"], "idle");
        assert_eq!(state["messageCount"], if fail || abort { 0 } else { 2 });
        assert_eq!(writer.close(output_task).await.is_err(), fail);
        timeout(Duration::from_secs(1), gate.dropped.notified())
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn dropping_the_writer_task_releases_pending_output() {
    let gate = Arc::new(FlushGate::default());
    let (writer, task) = Writer::start(
        HeldTerminal {
            gate: gate.clone(),
            fail: false,
            terminal: false,
        },
        CancellationToken::new(),
    );
    let receipt = writer.enqueue(json!({"type":"agent_end"})).unwrap();
    timeout(Duration::from_secs(1), gate.reached.notified())
        .await
        .unwrap();
    drop(task);
    assert!(timeout(
        Duration::from_secs(1),
        writer.wait(receipt, &CancellationToken::new())
    )
    .await
    .unwrap()
    .is_err());
    timeout(Duration::from_secs(1), gate.dropped.notified())
        .await
        .unwrap();
}
