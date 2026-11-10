use crate::{
    CancellationToken, Error, Event, EventKind, Result, RunError, RunOutcome, MAX_JSONL_LINE_BYTES,
};
use bytes::Bytes;
use hyper::body::{Body as HttpBody, Frame, SizeHint};
use std::{
    convert::Infallible,
    pin::Pin,
    task::{Context, Poll},
    time::Duration,
};
use tokio::sync::{mpsc, watch};

pub(super) struct Output {
    bytes: Bytes,
    terminal: bool,
}

pub(super) enum Body {
    Fixed(Option<Bytes>),
    Stream {
        receiver: mpsc::Receiver<Output>,
        ended: bool,
    },
}

impl HttpBody for Body {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<std::result::Result<Frame<Bytes>, Infallible>>> {
        match self.get_mut() {
            Self::Fixed(bytes) => Poll::Ready(bytes.take().map(|bytes| Ok(Frame::data(bytes)))),
            Self::Stream { receiver, ended } => {
                if *ended {
                    return Poll::Ready(None);
                }
                match receiver.poll_recv(cx) {
                    Poll::Ready(Some(output)) => {
                        *ended = output.terminal;
                        Poll::Ready(Some(Ok(Frame::data(output.bytes))))
                    }
                    Poll::Ready(None) => {
                        *ended = true;
                        Poll::Ready(None)
                    }
                    Poll::Pending => Poll::Pending,
                }
            }
        }
    }

    fn is_end_stream(&self) -> bool {
        match self {
            Self::Fixed(bytes) => bytes.is_none(),
            Self::Stream { ended, .. } => *ended,
        }
    }

    fn size_hint(&self) -> SizeHint {
        match self {
            Self::Fixed(bytes) => {
                SizeHint::with_exact(bytes.as_ref().map_or(0, |bytes| bytes.len() as u64))
            }
            Self::Stream { .. } => SizeHint::default(),
        }
    }
}

#[derive(Clone)]
pub(super) struct ConnectionContext {
    pub cancellation: CancellationToken,
    pub written: watch::Receiver<Option<bool>>,
}

pub(super) async fn send_event(
    mut event: Event,
    sender: mpsc::Sender<Output>,
    context: ConnectionContext,
) -> Result<()> {
    let mut data =
        serde_json::to_vec(&event).map_err(|error| Error::Consumer(error.to_string()))?;
    let oversized = data.len() > MAX_JSONL_LINE_BYTES;
    if oversized {
        context.cancellation.cancel();
        event.kind = EventKind::AgentEnd {
            outcome: RunOutcome::Failed {
                error: RunError {
                    code: "output_too_large".into(),
                    message: format!("HTTP output exceeds {MAX_JSONL_LINE_BYTES} UTF-8 bytes"),
                    retryable: false,
                },
            },
        };
        data = serde_json::to_vec(&event).unwrap();
    }
    let terminal = matches!(event.kind, EventKind::AgentEnd { .. });
    data.push(b'\n');
    let write = async {
        sender
            .send(Output {
                bytes: data.into(),
                terminal,
            })
            .await
            .map_err(|_| Error::Consumer("HTTP response disconnected".into()))?;
        if terminal {
            // Taking the final frame out of the channel does not prove that
            // Hyper wrote it. Only connection completion permits a commit.
            let mut written = context.written.clone();
            let success = *written
                .wait_for(|result| result.is_some())
                .await
                .map_err(|_| Error::Consumer("HTTP connection ended before completion".into()))?;
            if success != Some(true) {
                return Err(Error::Consumer(
                    "HTTP response did not finish writing".into(),
                ));
            }
        }
        Ok(())
    };
    tokio::pin!(write);
    let result = tokio::select! {
        result = &mut write => result,
        _ = context.cancellation.cancelled() => {
            tokio::time::timeout(Duration::from_secs(1), &mut write).await
                .map_err(|_| Error::Consumer("HTTP output did not drain after cancellation".into()))?
        }
    };
    result?;
    if oversized {
        return Err(Error::Consumer("HTTP output exceeds its byte limit".into()));
    }
    Ok(())
}
