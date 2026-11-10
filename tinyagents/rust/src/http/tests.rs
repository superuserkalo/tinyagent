use super::*;
use crate::{
    bash_tool, AgentConfig, Error, Message, ProviderEvent, ProviderRequest, ProviderStream,
    ProviderTurn, Result, Tool, ToolCall, ToolDefinition, MAX_JSONL_LINE_BYTES,
};
use bytes::Bytes;
use futures_util::{stream, StreamExt};
use http_body_util::{combinators::BoxBody, Full, StreamBody};
use hyper::{body::Frame, HeaderMap};
use hyper_util::rt::TokioIo;
use std::{
    convert::Infallible,
    io,
    pin::Pin,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
    task::{Context, Poll},
};
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt, ReadBuf},
    task::JoinHandle,
    time::timeout,
};

struct Fixture {
    produced: Arc<AtomicUsize>,
}

impl Provider for Fixture {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        let prompt = request
            .history
            .iter()
            .rev()
            .find_map(|message| match message {
                Message::User { text } => Some(text.as_str()),
                _ => None,
            })
            .unwrap();
        if prompt == "fail_before" {
            return Err(Error::Provider("fixture failure".into()));
        }
        if prompt == "wait" {
            return Ok(Box::pin(
                stream::iter([Ok(ProviderEvent::TextDelta("waiting".into()))])
                    .chain(stream::pending()),
            ));
        }
        if prompt == "fail" {
            return Ok(Box::pin(stream::iter([
                Ok(ProviderEvent::TextDelta("partial".into())),
                Err(Error::Provider("fixture failure".into())),
            ])));
        }
        if prompt == "oversized" {
            return Ok(Box::pin(stream::iter([Ok(ProviderEvent::TextDelta(
                "x".repeat(MAX_JSONL_LINE_BYTES),
            ))])));
        }
        if prompt == "flood" {
            let produced = self.produced.clone();
            return Ok(Box::pin(stream::repeat_with(move || {
                produced.fetch_add(1, Ordering::SeqCst);
                Ok(ProviderEvent::TextDelta("x".repeat(64 * 1024)))
            })));
        }
        let tool_calls = if !matches!(request.history.last(), Some(Message::ToolResult { .. })) {
            match prompt {
                "wait_tool" => vec![ToolCall {
                    call_id: "bash-1".into(),
                    name: "bash".into(),
                    arguments: json!({"command":"sleep 3 &"}),
                }],
                "cleanup" => vec![ToolCall {
                    call_id: "slow-1".into(),
                    name: "slow".into(),
                    arguments: json!({}),
                }],
                _ => vec![],
            }
        } else {
            vec![]
        };
        let turn = ProviderTurn {
            text: "done\nfixture".into(),
            tool_calls,
            replay: vec![],
            output: vec![],
        };
        Ok(Box::pin(stream::iter([
            Ok(ProviderEvent::TextDelta("done\nfixture".into())),
            Ok(ProviderEvent::Turn(turn)),
        ])))
    }
}

fn state(token: &str) -> Arc<HttpState<Fixture>> {
    Arc::new(HttpState::new(
        Arc::new(
            Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                provider: Fixture {
                    produced: Arc::new(AtomicUsize::new(0)),
                },
                tools: vec![bash_tool().unwrap()],
            })
            .unwrap(),
        ),
        token.into(),
    ))
}

type RequestBody = BoxBody<Bytes, Infallible>;

fn request(method: &str, path: &str, bytes: impl Into<Bytes>) -> Request<RequestBody> {
    Request::builder()
        .method(method)
        .uri(path)
        .header("Host", "localhost")
        .header(CONTENT_TYPE, "application/json")
        .body(Full::new(bytes.into()).boxed())
        .unwrap()
}

struct Exchange {
    response: Response<Incoming>,
    connection: JoinHandle<std::result::Result<(), hyper::Error>>,
    pending: Vec<u8>,
}

impl Drop for Exchange {
    fn drop(&mut self) {
        self.connection.abort();
    }
}

impl Exchange {
    async fn collect(&mut self) -> std::result::Result<Bytes, hyper::Error> {
        timeout(Duration::from_secs(3), self.response.body_mut().collect())
            .await
            .expect("HTTP body stalled")
            .map(|body| body.to_bytes())
    }

    async fn event(&mut self) -> Value {
        loop {
            if let Some(index) = self.pending.iter().position(|byte| *byte == b'\n') {
                let line = self.pending.drain(..=index).collect::<Vec<_>>();
                assert!(line.len() <= MAX_JSONL_LINE_BYTES + 1);
                return serde_json::from_slice(&line).unwrap();
            }
            let frame = timeout(Duration::from_secs(3), self.response.body_mut().frame())
                .await
                .unwrap()
                .expect("missing event")
                .unwrap();
            if let Ok(bytes) = frame.into_data() {
                self.pending.extend_from_slice(&bytes);
            }
        }
    }

    async fn until(&mut self, kind: &str) -> Vec<Value> {
        let mut events = Vec::new();
        loop {
            let event = self.event().await;
            let done = event["type"] == kind;
            events.push(event);
            if done {
                return events;
            }
        }
    }
}

// A real Hyper connection over bounded in-memory I/O verifies HTTP parsing and
// framing without claiming that the operating system's TCP path was exercised.
async fn exchange(state: &Arc<HttpState<Fixture>>, request: Request<RequestBody>) -> Exchange {
    exchange_with_wire(state, request, None).await.unwrap()
}

async fn exchange_with_wire(
    state: &Arc<HttpState<Fixture>>,
    request: Request<RequestBody>,
    failure: Option<Arc<AtomicBool>>,
) -> std::result::Result<Exchange, hyper::Error> {
    let (client, server) = tokio::io::duplex(1024);
    let owner = state.clone();
    state.tasks.spawn(async move {
        owner
            .connection(Wire {
                inner: server,
                fail_terminal: failure,
            })
            .await;
    });
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(client))
        .await
        .unwrap();
    let connection = tokio::spawn(connection);
    match timeout(Duration::from_secs(3), sender.send_request(request))
        .await
        .expect("HTTP headers stalled")
    {
        Ok(response) => Ok(Exchange {
            response,
            connection,
            pending: Vec::new(),
        }),
        Err(error) => {
            connection.abort();
            Err(error)
        }
    }
}

struct Wire {
    inner: tokio::io::DuplexStream,
    fail_terminal: Option<Arc<AtomicBool>>,
}

impl AsyncRead for Wire {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_read(cx, buffer)
    }
}

impl AsyncWrite for Wire {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        bytes: &[u8],
    ) -> Poll<io::Result<usize>> {
        if let Some(failed) = &self.fail_terminal {
            if bytes
                .windows(b"agent_end".len())
                .any(|part| part == b"agent_end")
            {
                failed.store(true, Ordering::SeqCst);
                return Poll::Ready(Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "fixture terminal write failure",
                )));
            }
        }
        Pin::new(&mut self.inner).poll_write(cx, bytes)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.inner).poll_shutdown(cx)
    }
}

async fn complete(
    state: &Arc<HttpState<Fixture>>,
    request: Request<RequestBody>,
) -> (StatusCode, HeaderMap, Value) {
    let mut exchange = exchange(state, request).await;
    let status = exchange.response.status();
    let headers = exchange.response.headers().clone();
    let bytes = exchange.collect().await.unwrap();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, headers, body)
}

async fn create(state: &Arc<HttpState<Fixture>>) -> String {
    let (status, _, body) = complete(state, request("POST", "/sessions", "")).await;
    assert_eq!(status, StatusCode::CREATED);
    body["sessionId"].as_str().unwrap().to_owned()
}

async fn prompt(state: &Arc<HttpState<Fixture>>, id: &str, message: &str) -> Exchange {
    let exchange = exchange(
        state,
        request(
            "POST",
            &format!("/sessions/{id}/prompts"),
            json!({"message":message}).to_string(),
        ),
    )
    .await;
    assert_eq!(exchange.response.status(), StatusCode::OK);
    assert_eq!(
        exchange.response.headers()[CONTENT_TYPE],
        "application/jsonl; charset=utf-8"
    );
    exchange
}

async fn idle(state: &HttpState<Fixture>, id: &str, expected: usize) {
    let session = state.sessions.lock().unwrap().values[id].clone();
    timeout(Duration::from_secs(2), async {
        loop {
            let current = serde_json::to_value(session.state()).unwrap();
            if current["status"] == "idle" {
                assert_eq!(current["messageCount"], expected, "{current}");
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("session did not settle");
}

#[tokio::test]
async fn health_routes_and_authentication() {
    let state = state("secret");
    for auth in [None, Some("Bearer wrong")] {
        let mut req = request("GET", "/health", "");
        if let Some(auth) = auth {
            req.headers_mut()
                .insert(AUTHORIZATION, auth.parse().unwrap());
        }
        let (status, headers, body) = complete(&state, req).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(headers[WWW_AUTHENTICATE], "Bearer");
        assert_eq!(body["error"]["code"], "unauthorized");
        assert!(!headers.contains_key("access-control-allow-origin"));
    }
    for (method, path, status) in [
        ("GET", "/health", 200),
        ("OPTIONS", "/sessions", 404),
        ("GET", "/missing", 404),
    ] {
        let mut req = request(method, path, "");
        req.headers_mut()
            .insert(AUTHORIZATION, "Bearer secret".parse().unwrap());
        let (actual, headers, body) = complete(&state, req).await;
        assert_eq!(actual.as_u16(), status);
        assert!(!headers.contains_key("access-control-allow-origin"));
        if status == 200 {
            assert_eq!(body, json!({"status":"ok","protocolVersion":1}));
        }
    }
    let mut req = request("GET", "/health", "");
    req.headers_mut()
        .append(AUTHORIZATION, "Bearer secret".parse().unwrap());
    req.headers_mut()
        .append(AUTHORIZATION, "Bearer secret".parse().unwrap());
    assert_eq!(complete(&state, req).await.0, StatusCode::UNAUTHORIZED);
    state.shutdown().await;
}

#[tokio::test]
async fn body_validation_and_known_and_chunked_limits() {
    let state = state("");
    let id = create(&state).await;
    let path = format!("/sessions/{id}/prompts");
    for (body, content_type, status, code) in [
        (&b"{}"[..], None, 415, "unsupported_media_type"),
        (
            &b"{}"[..],
            Some("application/json; charset=latin1"),
            415,
            "unsupported_media_type",
        ),
        (&b"\xff"[..], Some("application/json"), 400, "invalid_utf8"),
        (&b"{"[..], Some("application/json"), 400, "invalid_request"),
        (&b"[]"[..], Some("application/json"), 400, "invalid_request"),
        (
            &b"null"[..],
            Some("application/json"),
            400,
            "invalid_request",
        ),
        (&b"{}"[..], Some("application/json"), 400, "invalid_request"),
        (
            &b"{\"message\":\"\"}"[..],
            Some("application/json"),
            400,
            "invalid_request",
        ),
    ] {
        let mut req = request("POST", &path, Bytes::copy_from_slice(body));
        req.headers_mut().remove(CONTENT_TYPE);
        if let Some(content_type) = content_type {
            req.headers_mut()
                .insert(CONTENT_TYPE, content_type.parse().unwrap());
        }
        let (actual, _, result) = complete(&state, req).await;
        assert_eq!(actual.as_u16(), status);
        assert_eq!(result["error"]["code"], code);
    }
    for chunked in [false, true] {
        let mut req = request("POST", &path, vec![b' '; MAX_HTTP_BODY_BYTES + 1]);
        if chunked {
            *req.body_mut() = BodyExt::boxed(StreamBody::new(stream::iter([
                Ok::<_, Infallible>(Frame::data(Bytes::from(vec![b' '; MAX_HTTP_BODY_BYTES]))),
                Ok(Frame::data(Bytes::from_static(b" "))),
            ])));
        }
        let (status, _, body) = complete(&state, req).await;
        assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(body["error"]["code"], "body_too_large");
    }
    assert_eq!(
        complete(&state, request("POST", "/sessions/missing/prompts", "{}"))
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        complete(
            &state,
            request("POST", &format!("/sessions/{id}/cancel"), "")
        )
        .await
        .2,
        json!({"accepted":false,"runId":null})
    );
    idle(&state, &id, 0).await;
    state.shutdown().await;
}

#[tokio::test]
async fn completion_order_and_failed_and_oversized_runs_preserve_history() {
    let state = state("");
    let id = create(&state).await;
    let mut first = prompt(&state, &id, "finish").await;
    let events = first.until("agent_end").await;
    assert_eq!(events.len(), 3);
    for (index, event) in events.iter().enumerate() {
        assert_eq!(event["sequence"], index + 1);
        assert_eq!(event["runId"], events[0]["runId"]);
    }
    assert_eq!(events[0]["type"], "agent_start");
    assert_eq!(events[1]["delta"], "done\nfixture");
    assert_eq!(events[2]["status"], "completed");
    assert!(first.collect().await.unwrap().is_empty());
    idle(&state, &id, 2).await;
    for message in ["fail_before", "fail", "oversized"] {
        let mut failed = prompt(&state, &id, message).await;
        let events = failed.until("agent_end").await;
        let end = events.last().unwrap();
        assert_eq!(end["status"], "failed");
        assert_eq!(
            end["error"]["code"],
            if message == "oversized" {
                "output_too_large"
            } else {
                "provider_error"
            }
        );
        assert!(failed.collect().await.unwrap().is_empty());
        idle(&state, &id, 2).await;
    }
    state.shutdown().await;
}

#[tokio::test]
async fn terminal_write_failure_and_disconnect_roll_back_history() {
    let state = state("");
    let id = create(&state).await;
    let mut first = prompt(&state, &id, "finish").await;
    first.collect().await.unwrap();
    idle(&state, &id, 2).await;
    let failure = Arc::new(AtomicBool::new(false));
    let req = request(
        "POST",
        &format!("/sessions/{id}/prompts"),
        json!({"message":"finish"}).to_string(),
    );
    if let Ok(mut exchange) = exchange_with_wire(&state, req, Some(failure.clone())).await {
        assert!(exchange.collect().await.is_err());
    }
    assert!(failure.load(Ordering::SeqCst));
    idle(&state, &id, 2).await;
    let mut waiting = prompt(&state, &id, "wait").await;
    waiting.until("text_delta").await;
    drop(waiting);
    idle(&state, &id, 2).await;
    state.shutdown().await;
}

#[tokio::test]
async fn sessions_run_independently_cancel_and_delete_waits_for_tool_cleanup() {
    let cleaned = Arc::new(AtomicBool::new(false));
    let flag = cleaned.clone();
    let tool = Tool::new(
        ToolDefinition {
            name: "slow".into(),
            description: String::new(),
            parameters: json!({"type":"object"}),
        },
        move |_, token| {
            let flag = flag.clone();
            async move {
                token.cancelled().await;
                tokio::time::sleep(Duration::from_millis(40)).await;
                flag.store(true, Ordering::SeqCst);
                Err(Error::Cancelled)
            }
        },
    )
    .unwrap();
    let state = Arc::new(HttpState::new(
        Arc::new(
            Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                provider: Fixture {
                    produced: Arc::new(AtomicUsize::new(0)),
                },
                tools: vec![tool, bash_tool().unwrap()],
            })
            .unwrap(),
        ),
        String::new(),
    ));
    let (first, second) = (create(&state).await, create(&state).await);
    let mut waiting = prompt(&state, &first, "wait_tool").await;
    waiting.until("tool_execution_start").await;
    let mut other = prompt(&state, &second, "finish").await;
    other.collect().await.unwrap();
    idle(&state, &second, 2).await;
    let busy = complete(
        &state,
        request(
            "POST",
            &format!("/sessions/{first}/prompts"),
            "{\"message\":\"busy\"}",
        ),
    )
    .await;
    assert_eq!(busy.0, StatusCode::CONFLICT);
    assert_eq!(busy.2["error"]["code"], "session_busy");
    let cancel = complete(
        &state,
        request("POST", &format!("/sessions/{first}/cancel"), ""),
    )
    .await;
    assert_eq!(cancel.2["accepted"], true);
    let events = waiting.until("agent_end").await;
    assert_eq!(events[0]["type"], "tool_execution_end");
    assert_eq!(events[0]["isError"], true);
    assert_eq!(events.last().unwrap()["status"], "aborted");
    waiting.collect().await.unwrap();
    idle(&state, &first, 0).await;
    let mut cleanup = prompt(&state, &first, "cleanup").await;
    cleanup.until("tool_execution_start").await;
    let deleted = complete(&state, request("DELETE", &format!("/sessions/{first}"), "")).await;
    assert_eq!(deleted.0, StatusCode::NO_CONTENT);
    assert!(cleaned.load(Ordering::SeqCst));
    assert_eq!(
        cleanup.until("agent_end").await.last().unwrap()["status"],
        "aborted"
    );
    assert_eq!(
        complete(&state, request("DELETE", &format!("/sessions/{first}"), ""))
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    state.shutdown().await;
}

#[tokio::test]
async fn blocked_output_cancellation_and_server_shutdown_settle() {
    let produced = Arc::new(AtomicUsize::new(0));
    let state = Arc::new(HttpState::new(
        Arc::new(
            Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                provider: Fixture {
                    produced: produced.clone(),
                },
                tools: vec![],
            })
            .unwrap(),
        ),
        String::new(),
    ));
    let id = create(&state).await;
    let _blocked = prompt(&state, &id, "flood").await;
    tokio::time::sleep(Duration::from_millis(30)).await;
    let count = produced.load(Ordering::SeqCst);
    assert!(count <= 8, "blocked reader allowed {count} 64-KiB events");
    let cancel = complete(
        &state,
        request("POST", &format!("/sessions/{id}/cancel"), ""),
    )
    .await;
    assert_eq!(cancel.2["accepted"], true);
    idle(&state, &id, 0).await;
    timeout(Duration::from_millis(500), async {
        while !state.tasks.is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("cancelled run left a blocked server connection");
    let mut waiting = prompt(&state, &id, "wait").await;
    waiting.until("text_delta").await;
    timeout(Duration::from_secs(2), state.shutdown())
        .await
        .expect("shutdown did not settle");
    assert_eq!(
        waiting.until("agent_end").await.last().unwrap()["status"],
        "aborted"
    );
    assert!(state.sessions.lock().unwrap().values.is_empty());
}

#[tokio::test]
async fn disconnected_deletion_releases_the_session_and_finishes_cleanup() {
    let cleaned = Arc::new(AtomicBool::new(false));
    let flag = cleaned.clone();
    let tool = Tool::new(
        ToolDefinition {
            name: "slow".into(),
            description: String::new(),
            parameters: json!({"type":"object"}),
        },
        move |_, token| {
            let flag = flag.clone();
            async move {
                token.cancelled().await;
                tokio::time::sleep(Duration::from_millis(50)).await;
                flag.store(true, Ordering::SeqCst);
                Err(Error::Cancelled)
            }
        },
    )
    .unwrap();
    let state = Arc::new(HttpState::new(
        Arc::new(
            Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                provider: Fixture {
                    produced: Arc::new(AtomicUsize::new(0)),
                },
                tools: vec![tool],
            })
            .unwrap(),
        ),
        String::new(),
    ));
    let id = create(&state).await;
    let mut active = prompt(&state, &id, "cleanup").await;
    active.until("tool_execution_start").await;
    let (mut peer, server) = tokio::io::duplex(1024);
    let owner = state.clone();
    state.tasks.spawn(async move {
        owner.connection(server).await;
    });
    peer.write_all(
        format!("DELETE /sessions/{id} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
            .as_bytes(),
    )
    .await
    .unwrap();
    timeout(Duration::from_secs(1), async {
        while state.sessions.lock().unwrap().values.contains_key(&id) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(!cleaned.load(Ordering::SeqCst));
    drop(peer);
    assert_eq!(
        active.until("agent_end").await.last().unwrap()["status"],
        "aborted"
    );
    assert!(cleaned.load(Ordering::SeqCst));
    assert!(!state.sessions.lock().unwrap().values.contains_key(&id));
    state.shutdown().await;
}
