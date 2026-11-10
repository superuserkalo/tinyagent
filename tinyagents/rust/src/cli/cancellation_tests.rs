use super::interactive;
use crate::{
    Agent, AgentConfig, CancellationToken, Error, Message, Provider, ProviderEvent,
    ProviderRequest, ProviderStream, ProviderTurn, Result, Tool, ToolCall, ToolDefinition,
};
use futures_util::{stream, Stream};
use serde_json::json;
use std::{
    io,
    pin::Pin,
    sync::{
        atomic::{AtomicUsize, Ordering},
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

struct PausedWriter {
    allowed: usize,
    pause_flush: bool,
    blocked: Arc<Notify>,
    dropped: Arc<Notify>,
}

impl AsyncWrite for PausedWriter {
    fn poll_write(
        mut self: Pin<&mut Self>,
        _: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        if self.allowed == 0 {
            if self.pause_flush {
                return Poll::Ready(Ok(bytes.len()));
            }
            self.blocked.notify_one();
            Poll::Pending
        } else {
            self.allowed -= 1;
            Poll::Ready(Ok(bytes.len()))
        }
    }

    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        if self.pause_flush && self.allowed == 0 {
            self.blocked.notify_one();
            return Poll::Pending;
        }
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

impl Drop for PausedWriter {
    fn drop(&mut self) {
        self.dropped.notify_one();
    }
}

struct TrackedStream {
    inner: ProviderStream,
    active: Arc<AtomicUsize>,
}

impl Stream for TrackedStream {
    type Item = Result<ProviderEvent>;
    fn poll_next(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.inner.as_mut().poll_next(context)
    }
}

impl Drop for TrackedStream {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::SeqCst);
    }
}

struct OutputProvider {
    active: Arc<AtomicUsize>,
}

impl Provider for OutputProvider {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        let Some(Message::User { text }) = request.history.last() else {
            panic!("unexpected continuation");
        };
        let mut events = Vec::new();
        if text == "error" {
            events.push(Err(Error::Provider("fixture failure".into())));
        } else {
            if text == "text" {
                events.push(Ok(ProviderEvent::TextDelta("hello".into())));
            }
            events.push(Ok(ProviderEvent::Turn(ProviderTurn {
                text: "done".into(),
                replay: vec![],
                output: vec![],
                tool_calls: if text == "tool" {
                    vec![ToolCall {
                        call_id: "one".into(),
                        name: "fixture".into(),
                        arguments: json!({"command":"wait"}),
                    }]
                } else {
                    vec![]
                },
            })));
        }
        self.active.fetch_add(1, Ordering::SeqCst);
        Ok(Box::pin(TrackedStream {
            inner: Box::pin(stream::iter(events)),
            active: self.active.clone(),
        }))
    }
}

#[tokio::test]
async fn cancellation_releases_every_interactive_output_site() {
    for (name, prompt, stdout_writes, stderr_writes, expected_tools) in [
        ("banner", "text", 0, usize::MAX, 0),
        ("prompt", "text", 1, usize::MAX, 0),
        ("text delta", "text", 2, usize::MAX, 0),
        ("final text", "quiet", 2, usize::MAX, 0),
        ("tool start", "tool", usize::MAX, 0, 0),
        ("tool end", "tool", usize::MAX, 1, 1),
        ("provider error", "error", usize::MAX, 0, 0),
    ] {
        let blocked = Arc::new(Notify::new());
        let output_dropped = Arc::new(Notify::new());
        let diagnostics_dropped = Arc::new(Notify::new());
        let active = Arc::new(AtomicUsize::new(0));
        let tools = Arc::new(AtomicUsize::new(0));
        let calls = tools.clone();
        let tool = Tool::new(
            ToolDefinition {
                name: "fixture".into(),
                description: String::new(),
                parameters: json!({"type":"object"}),
            },
            move |_, _| {
                calls.fetch_add(1, Ordering::SeqCst);
                async { Ok("observation".into()) }
            },
        )
        .unwrap();
        let agent = Agent::new(AgentConfig {
            model: "fixture".into(),
            instructions: String::new(),
            provider: OutputProvider {
                active: active.clone(),
            },
            tools: vec![tool],
        })
        .unwrap();
        let cancellation = CancellationToken::new();
        let input = format!("{prompt}\n");
        let output = PausedWriter {
            allowed: stdout_writes,
            pause_flush: false,
            blocked: blocked.clone(),
            dropped: output_dropped.clone(),
        };
        let diagnostics = PausedWriter {
            allowed: stderr_writes,
            pause_flush: false,
            blocked: blocked.clone(),
            dropped: diagnostics_dropped.clone(),
        };
        let run = interactive(
            &agent,
            input.as_bytes(),
            output,
            diagnostics,
            cancellation.clone(),
        );
        let cancel = async {
            timeout(Duration::from_secs(1), blocked.notified())
                .await
                .expect(name);
            cancellation.cancel();
        };
        let (result, ()) = timeout(Duration::from_millis(1500), async {
            tokio::join!(run, cancel)
        })
        .await
        .expect(name);
        assert!(
            matches!(result, Err(Error::Consumer(_))),
            "{name}: {result:?}"
        );
        assert_eq!(active.load(Ordering::SeqCst), 0, "{name}: provider leaked");
        assert_eq!(tools.load(Ordering::SeqCst), expected_tools, "{name}");
        timeout(Duration::from_secs(1), output_dropped.notified())
            .await
            .expect(name);
        timeout(Duration::from_secs(1), diagnostics_dropped.notified())
            .await
            .expect(name);
    }
}

#[tokio::test]
async fn self_test_output_obeys_cancellation() {
    for pause_flush in [false, true] {
        let blocked = Arc::new(Notify::new());
        let cancellation = CancellationToken::new();
        let output = PausedWriter {
            allowed: 0,
            pause_flush,
            blocked: blocked.clone(),
            dropped: Arc::new(Notify::new()),
        };
        let args = ["--self-test".into()];
        let run = super::run(
            &args,
            tokio::io::empty(),
            output,
            tokio::io::sink(),
            cancellation.clone(),
        );
        let cancel = async {
            timeout(Duration::from_secs(1), blocked.notified())
                .await
                .unwrap();
            cancellation.cancel();
        };
        let (result, ()) = timeout(Duration::from_millis(1500), async {
            tokio::join!(run, cancel)
        })
        .await
        .expect("self-test output ignored cancellation");
        assert!(matches!(result, Err(Error::Consumer(_))));
    }
}

#[tokio::test]
async fn pending_output_can_drain_after_cancellation_without_restarting_the_write() {
    let (output, mut reader) = tokio::io::duplex(1);
    let cancellation = CancellationToken::new();
    let args = ["--self-test".into()];
    let run = super::run(
        &args,
        tokio::io::empty(),
        output,
        tokio::io::sink(),
        cancellation.clone(),
    );
    let read = async {
        let mut first = [0];
        reader.read_exact(&mut first).await.unwrap();
        cancellation.cancel();
        let mut bytes = first.to_vec();
        reader.read_to_end(&mut bytes).await.unwrap();
        bytes
    };
    let (result, bytes) = timeout(Duration::from_secs(1), async { tokio::join!(run, read) })
        .await
        .unwrap();
    result.unwrap();
    assert_eq!(bytes, b"self-test passed\n");
}
