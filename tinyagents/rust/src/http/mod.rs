mod body;
mod server;

pub use server::{serve, HttpOptions, HttpServer};

use crate::{
    session::{Session, StartError},
    Agent, CancellationToken, Provider, RunError, MAX_HTTP_BODY_BYTES, PROTOCOL_VERSION,
};
use body::{send_event, Body, ConnectionContext};
use http_body_util::BodyExt;
use hyper::{
    body::Incoming,
    header::{AUTHORIZATION, CACHE_CONTROL, CONTENT_LENGTH, CONTENT_TYPE, WWW_AUTHENTICATE},
    Request, Response, StatusCode,
};
use serde_json::{json, Map, Value};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use subtle::ConstantTimeEq;
use tokio::sync::mpsc;
use tokio_util::task::TaskTracker;
use uuid::Uuid;

struct HttpState<P> {
    agent: Arc<Agent<P>>,
    authorization: Option<String>,
    sessions: Mutex<Sessions>,
    cancellation: CancellationToken,
    tasks: TaskTracker,
}

#[derive(Default)]
struct Sessions {
    values: BTreeMap<String, Arc<Session>>,
    closing: bool,
}

struct HttpError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
}

impl HttpError {
    fn new(status: StatusCode, code: &'static str, message: &'static str) -> Self {
        Self {
            status,
            code,
            message,
        }
    }

    fn response(self) -> Response<Body> {
        let mut response = json_response(
            self.status,
            json!({"error":RunError {
                code: self.code.into(), message: self.message.into(),
                retryable: self.status == StatusCode::CONFLICT || self.status.is_server_error(),
            }}),
        );
        if self.status == StatusCode::UNAUTHORIZED {
            response
                .headers_mut()
                .insert(WWW_AUTHENTICATE, "Bearer".parse().unwrap());
        }
        response
    }
}

impl<P: Provider + 'static> HttpState<P> {
    fn new(agent: Arc<Agent<P>>, token: String) -> Self {
        Self {
            agent,
            authorization: if token.is_empty() {
                None
            } else {
                Some(format!("Bearer {token}"))
            },
            sessions: Mutex::new(Sessions::default()),
            cancellation: CancellationToken::new(),
            tasks: TaskTracker::new(),
        }
    }

    async fn handle(
        self: Arc<Self>,
        request: Request<Incoming>,
        context: ConnectionContext,
    ) -> Response<Body> {
        self.dispatch(request, context)
            .await
            .unwrap_or_else(HttpError::response)
    }

    async fn dispatch(
        &self,
        request: Request<Incoming>,
        context: ConnectionContext,
    ) -> std::result::Result<Response<Body>, HttpError> {
        if let Some(expected) = &self.authorization {
            let mut values = request.headers().get_all(AUTHORIZATION).iter();
            let actual = values.next().map_or(&b""[..], |value| value.as_bytes());
            if values.next().is_some() || !bool::from(actual.ct_eq(expected.as_bytes())) {
                return Err(HttpError::new(
                    StatusCode::UNAUTHORIZED,
                    "unauthorized",
                    "Missing or invalid bearer token",
                ));
            }
        }
        if self.sessions.lock().unwrap().closing {
            return Err(HttpError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "server_closed",
                "Server is shutting down",
            ));
        }
        let method = request.method().clone();
        let path = request.uri().path().to_owned();
        if method == hyper::Method::GET && path == "/health" {
            return Ok(json_response(
                StatusCode::OK,
                json!({"status":"ok","protocolVersion":PROTOCOL_VERSION}),
            ));
        }
        if method == hyper::Method::POST && path == "/sessions" {
            read_object(request, true).await?;
            let id = format!("s_{}", Uuid::new_v4().simple());
            let mut sessions = self.sessions.lock().unwrap();
            if sessions.closing {
                return Err(HttpError::new(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "server_closed",
                    "Server is shutting down",
                ));
            }
            sessions
                .values
                .insert(id.clone(), Arc::new(Session::default()));
            return Ok(json_response(StatusCode::CREATED, json!({"sessionId":id})));
        }
        let parts = path.trim_start_matches('/').split('/').collect::<Vec<_>>();
        if !(2..=3).contains(&parts.len()) || parts[0] != "sessions" {
            return Err(HttpError::new(
                StatusCode::NOT_FOUND,
                "not_found",
                "Unknown route",
            ));
        }
        let session = self
            .sessions
            .lock()
            .unwrap()
            .values
            .get(parts[1])
            .cloned()
            .ok_or_else(|| {
                HttpError::new(
                    StatusCode::NOT_FOUND,
                    "session_not_found",
                    "Unknown session",
                )
            })?;
        if method == hyper::Method::DELETE && parts.len() == 2 {
            self.sessions.lock().unwrap().values.remove(parts[1]);
            // Remove ownership before awaiting cleanup so a disconnected
            // deletion request cannot leave a closed session in the map.
            session.close().await;
            return Ok(Response::builder()
                .status(StatusCode::NO_CONTENT)
                .body(Body::Fixed(None))
                .unwrap());
        }
        if method == hyper::Method::POST && parts.len() == 3 {
            match parts[2] {
                "cancel" => {
                    read_object(request, true).await?;
                    let run_id = session.abort();
                    return Ok(json_response(
                        StatusCode::OK,
                        json!({"accepted":run_id.is_some(),"runId":run_id}),
                    ));
                }
                "prompts" => return self.prompt(request, session, context).await,
                _ => {}
            }
        }
        Err(HttpError::new(
            StatusCode::NOT_FOUND,
            "not_found",
            "Unknown route",
        ))
    }

    async fn prompt(
        &self,
        request: Request<Incoming>,
        session: Arc<Session>,
        context: ConnectionContext,
    ) -> std::result::Result<Response<Body>, HttpError> {
        let object = read_object(request, false).await?;
        let message = object
            .get("message")
            .and_then(Value::as_str)
            .filter(|message| !message.is_empty())
            .ok_or_else(|| {
                HttpError::new(
                    StatusCode::BAD_REQUEST,
                    "invalid_request",
                    "message must be a non-empty string",
                )
            })?
            .to_owned();
        let run = session
            .begin(&context.cancellation)
            .map_err(|error| match error {
                StartError::Busy => HttpError::new(
                    StatusCode::CONFLICT,
                    "session_busy",
                    "The session already has an active run",
                ),
                StartError::Closed => HttpError::new(
                    StatusCode::NOT_FOUND,
                    "session_not_found",
                    "Unknown session",
                ),
            })?;
        let connection_cancellation = context.cancellation.clone();
        let run_cancellation = run.cancellation.clone();
        let context = ConnectionContext {
            cancellation: run.cancellation.clone(),
            written: context.written,
        };
        // One queued event plus one in-flight event stays below the shared
        // 4 MiB queue limit. The producer waits when the peer stops reading.
        let (sender, receiver) = mpsc::channel(1);
        let agent = self.agent.clone();
        self.tasks.spawn(async move {
            let execute = run.execute(&agent, &message, move |event| {
                send_event(event, sender.clone(), context.clone())
            });
            tokio::pin!(execute);
            let result = tokio::select! {
                biased;
                result = &mut execute => result,
                _ = run_cancellation.cancelled() => {
                    // A cancelled run must also release a socket whose peer
                    // stopped reading. The connection allows one final second
                    // for tool-end and terminal frames to drain.
                    connection_cancellation.cancel();
                    execute.await
                }
            };
            if matches!(result, Err(crate::Error::Consumer(_))) {
                connection_cancellation.cancel();
            }
        });
        Ok(Response::builder()
            .header(CONTENT_TYPE, "application/jsonl; charset=utf-8")
            .header(CACHE_CONTROL, "no-store")
            .body(Body::Stream {
                receiver,
                ended: false,
            })
            .unwrap())
    }

    async fn shutdown(&self) {
        self.cancellation.cancel();
        let sessions = {
            let mut sessions = self.sessions.lock().unwrap();
            sessions.closing = true;
            std::mem::take(&mut sessions.values)
        };
        for session in sessions.values() {
            session.abort();
        }
        for session in sessions.values() {
            session.close().await;
        }
        self.tasks.close();
        self.tasks.wait().await;
    }
}

async fn read_object(
    mut request: Request<Incoming>,
    optional: bool,
) -> std::result::Result<Map<String, Value>, HttpError> {
    let too_large = || {
        HttpError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "body_too_large",
            "Request body exceeds its byte limit",
        )
    };
    if request
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|length| length.to_str().ok())
        .and_then(|length| length.parse::<u64>().ok())
        .is_some_and(|length| length > MAX_HTTP_BODY_BYTES as u64)
    {
        return Err(too_large());
    }
    let read = async {
        let mut bytes = Vec::new();
        while let Some(frame) = request.body_mut().frame().await {
            let frame = frame.map_err(|_| {
                HttpError::new(
                    StatusCode::BAD_REQUEST,
                    "invalid_request",
                    "Could not read request body",
                )
            })?;
            if let Ok(data) = frame.into_data() {
                if bytes.len() + data.len() > MAX_HTTP_BODY_BYTES {
                    return Err(too_large());
                }
                bytes.extend_from_slice(&data);
            }
        }
        Ok(bytes)
    };
    let bytes = tokio::time::timeout(Duration::from_secs(30), read)
        .await
        .map_err(|_| {
            HttpError::new(
                StatusCode::BAD_REQUEST,
                "invalid_request",
                "Request body timed out",
            )
        })??;
    if optional && bytes.is_empty() {
        return Ok(Map::new());
    }
    let media_type = request
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<mime::Mime>().ok());
    if !media_type.is_some_and(|value| {
        value.type_() == mime::APPLICATION
            && value.subtype() == mime::JSON
            && value
                .get_param(mime::CHARSET)
                .is_none_or(|charset| charset.as_str().eq_ignore_ascii_case("utf-8"))
    }) {
        return Err(HttpError::new(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "Expected application/json with UTF-8 encoding",
        ));
    }
    let text = std::str::from_utf8(&bytes).map_err(|_| {
        HttpError::new(
            StatusCode::BAD_REQUEST,
            "invalid_utf8",
            "Request body is not valid UTF-8",
        )
    })?;
    serde_json::from_str(text).map_err(|_| {
        HttpError::new(
            StatusCode::BAD_REQUEST,
            "invalid_request",
            "Request body must be a JSON object",
        )
    })
}

fn json_response(status: StatusCode, value: Value) -> Response<Body> {
    let mut bytes = serde_json::to_vec(&value).unwrap();
    bytes.push(b'\n');
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, "application/json; charset=utf-8")
        .header(CACHE_CONTROL, "no-store")
        .body(Body::Fixed(Some(bytes.into())))
        .unwrap()
}

#[cfg(test)]
mod tests;
