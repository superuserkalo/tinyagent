use super::{anthropic, openai, parse, sse, AnthropicProvider, Bytes, OpenAIProvider};
use crate::{
    Agent, AgentConfig, CancellationToken, Error, EventKind, Message, Provider, ProviderEvent,
    ProviderRequest, ProviderStream, Result, RunOptions, Tool, ToolDefinition,
    MAX_JSONL_LINE_BYTES,
};
use futures_util::{stream, StreamExt, TryStreamExt};
use reqwest::Client;
use serde_json::{json, Value};
use std::{
    future::ready,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};

const OPENAI_TOOL: &str = include_str!("../../conformance/providers/openai_tool.sse");
const OPENAI_TEXT: &str = include_str!("../../conformance/providers/openai_text.sse");
const ANTHROPIC_TOOL: &str = include_str!("../../conformance/providers/anthropic_tool.sse");
const ANTHROPIC_TEXT: &str = include_str!("../../conformance/providers/anthropic_text.sse");

fn bytes(data: &[u8]) -> Bytes {
    // Split every UTF-8 sequence and SSE delimiter across transport chunks.
    stream::iter(data.iter().map(|byte| Ok(vec![*byte])).collect::<Vec<_>>()).boxed()
}

#[derive(Clone, Copy)]
enum Kind {
    OpenAI,
    Anthropic,
}

#[test]
fn provider_construction_needs_no_credentials() {
    assert_eq!(
        OpenAIProvider::new().unwrap().endpoint,
        "https://api.openai.com/v1/responses"
    );
    let anthropic = AnthropicProvider::new().unwrap();
    assert_eq!(anthropic.endpoint, "https://api.anthropic.com/v1/messages");
    assert_eq!(anthropic.max_tokens, 8192);
}

fn client() -> Client {
    Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap()
}

fn openai() -> OpenAIProvider {
    OpenAIProvider {
        client: client(),
        endpoint: "https://example.invalid/responses".into(),
        api_key: Some("fixture-key".into()),
    }
}

fn anthropic() -> AnthropicProvider {
    AnthropicProvider {
        client: client(),
        endpoint: "https://example.invalid/messages".into(),
        api_key: Some("fixture-key".into()),
        max_tokens: 8192,
    }
}

struct Fixture {
    kind: Kind,
    requests: Arc<Mutex<Vec<Value>>>,
}

impl Provider for Fixture {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        let first = request.history.len() == 1;
        let request = match self.kind {
            Kind::OpenAI => openai().request(request)?,
            Kind::Anthropic => anthropic().request(request)?,
        };
        assert_eq!(request.headers()["accept"], "text/event-stream");
        assert_eq!(request.headers()["content-type"], "application/json");
        match self.kind {
            Kind::OpenAI => assert_eq!(request.headers()["authorization"], "Bearer fixture-key"),
            Kind::Anthropic => {
                assert_eq!(request.headers()["x-api-key"], "fixture-key");
                assert_eq!(request.headers()["anthropic-version"], "2023-06-01");
            }
        }
        self.requests
            .lock()
            .unwrap()
            .push(serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap());
        Ok(match self.kind {
            Kind::OpenAI => parse(
                bytes(if first { OPENAI_TOOL } else { OPENAI_TEXT }.as_bytes()),
                openai::Response::default(),
            ),
            Kind::Anthropic => parse(
                bytes(
                    if first {
                        ANTHROPIC_TOOL
                    } else {
                        ANTHROPIC_TEXT
                    }
                    .as_bytes(),
                ),
                anthropic::Response::default(),
            ),
        })
    }
}

#[tokio::test]
async fn both_providers_complete_tool_loop_and_preserve_replay() {
    for kind in [Kind::OpenAI, Kind::Anthropic] {
        let requests = Arc::new(Mutex::new(Vec::new()));
        let tool = Tool::new(ToolDefinition { name: "echo".into(), description: "Echo text".into(),
            parameters: json!({"type":"object", "properties":{"text":{"type":"string"}}, "required":["text"]}),
        }, |arguments, _| { assert_eq!(arguments["text"], "café"); ready(Ok("echo:café".into())) }).unwrap();
        let agent = Agent::new(AgentConfig {
            model: "fixture".into(),
            instructions: "Use echo".into(),
            tools: vec![tool],
            provider: Fixture {
                kind,
                requests: requests.clone(),
            },
        })
        .unwrap();
        let mut deltas = String::new();
        let result = agent
            .run_with_events("go", RunOptions::default(), |event| {
                if let EventKind::TextDelta { delta } = event.kind {
                    deltas.push_str(&delta);
                }
                ready(Ok(()))
            })
            .await
            .unwrap();
        assert_eq!(result.text, "done\ncafé");
        assert!(deltas.ends_with("done\ncafé"));
        assert_eq!(result.history.len(), 4);
        let requests = requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert_eq!(requests[0]["stream"], true);
        assert_eq!(requests[0]["model"], "fixture");
        match kind {
            Kind::OpenAI => {
                assert_eq!(requests[0]["store"], false);
                for request in requests.iter() {
                    assert_eq!(request["include"], json!(["reasoning.encrypted_content"]));
                }
                assert_eq!(requests[0]["parallel_tool_calls"], false);
                assert_eq!(requests[0]["tools"][0]["strict"], true);
                assert_eq!(
                    requests[1]["input"][1]["encrypted_content"],
                    "opaque-fixture"
                );
                assert_eq!(requests[1]["input"][2]["arguments"], "{\"text\":\"café\"}");
                assert_eq!(requests[1]["input"][3]["output"], "echo:café");
            }
            Kind::Anthropic => {
                assert_eq!(requests[0]["max_tokens"], 8192);
                assert_eq!(
                    requests[1]["messages"][1]["content"][0]["signature"],
                    "signed-fixture"
                );
                assert_eq!(
                    requests[1]["messages"][1]["content"][1]["input"]["text"],
                    "café"
                );
                assert_eq!(
                    requests[1]["messages"][2]["content"][0]["content"],
                    "echo:café"
                );
                let Message::Assistant { replay, .. } = &result.history[1] else {
                    panic!("missing assistant")
                };
                assert_eq!(replay[0]["fixture_metadata"], "keep");
                assert_eq!(
                    replay[0]["usage"],
                    json!({"input_tokens":8,"output_tokens":12})
                );
            }
        }
    }
}

#[tokio::test]
async fn provider_terminal_does_not_wait_for_transport_eof() {
    for kind in [Kind::OpenAI, Kind::Anthropic] {
        let data = match kind {
            Kind::OpenAI => OPENAI_TEXT,
            Kind::Anthropic => ANTHROPIC_TEXT,
        };
        let chunks = bytes(data.as_bytes()).chain(stream::pending()).boxed();
        let events = match kind {
            Kind::OpenAI => parse(chunks, openai::Response::default()),
            Kind::Anthropic => parse(chunks, anthropic::Response::default()),
        };
        let values: Vec<ProviderEvent> =
            tokio::time::timeout(Duration::from_secs(1), events.try_collect())
                .await
                .unwrap()
                .unwrap();
        assert!(matches!(values.last(), Some(ProviderEvent::Turn(_))));
    }
}

#[tokio::test]
async fn sse_rejects_invalid_encoding_json_and_bounds() {
    for data in [
        b"data: \xff\n\n".to_vec(),
        b"data: {oops}\n\n".to_vec(),
        vec![b'x'; MAX_JSONL_LINE_BYTES + 1],
        format!(
            "data: {}\ndata: {}\n\n",
            "x".repeat(MAX_JSONL_LINE_BYTES / 2),
            "x".repeat(MAX_JSONL_LINE_BYTES / 2)
        )
        .into_bytes(),
    ] {
        let mut reader = sse::Reader::new(stream::iter([Ok(data)]).boxed());
        assert!(reader.next().await.is_err());
    }
    let mut reader = sse::Reader::new(bytes(
        b": comment\r\nevent: ignored\r\ndata: {\r\ndata: \"type\":\"fixture\"}\r\n\r\n",
    ));
    assert_eq!(
        reader.next().await.unwrap(),
        Some(json!({"type":"fixture"}))
    );
    assert_eq!(reader.next().await.unwrap(), None);
}

#[tokio::test]
async fn incomplete_and_malformed_openai_streams_fail() {
    for data in [
        "data: [DONE]\n\n".to_owned(),
        OPENAI_TOOL.replace("response.completed", "response.incomplete"),
        OPENAI_TOOL.replace("\"output_index\":1", "\"output_index\":-1"),
        OPENAI_TOOL.replace("caf", "caf\\q"),
        "data: {\"type\":\"response.completed\",\"response\":{\"output\":{}}}\n\n".into(),
        "data: {}\n\n".into(),
        "data: {\"type\":\"response.output_text.delta\",\"delta\":7}\n\n".into(),
    ] {
        assert!(
            parse(bytes(data.as_bytes()), openai::Response::default())
                .try_collect::<Vec<_>>()
                .await
                .is_err(),
            "{data}"
        );
    }
}

#[tokio::test]
async fn incomplete_and_malformed_anthropic_streams_fail() {
    for data in [
        ANTHROPIC_TEXT.replace("end_turn", "max_tokens"),
        ANTHROPIC_TEXT.replace("end_turn", "pause_turn"),
        ANTHROPIC_TOOL.replace("\"index\":1", "\"index\":-1"),
        ANTHROPIC_TOOL.replace("input_json_delta", "text_delta"),
        ANTHROPIC_TOOL.replace("caf", "caf\\q"),
        ANTHROPIC_TEXT.replace("data: {\"type\":\"content_block_stop\",\"index\":0}\n\n", ""),
        ANTHROPIC_TEXT.replace("data: {\"type\":\"message_stop\"}\n\n", ""),
        "data: {\"type\":\"message_stop\"}\n\n".into(),
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"late\"}}\n\n".into(),
    ] {
        assert!(parse(bytes(data.as_bytes()), anthropic::Response::default()).try_collect::<Vec<_>>().await.is_err(), "{data}");
    }
}

#[tokio::test]
async fn body_read_errors_propagate() {
    for kind in [Kind::OpenAI, Kind::Anthropic] {
        let chunks = stream::iter([Err(Error::Provider("reader failed".into()))]).boxed();
        let events = match kind {
            Kind::OpenAI => parse(chunks, openai::Response::default()),
            Kind::Anthropic => parse(chunks, anthropic::Response::default()),
        };
        assert_eq!(
            events.try_collect::<Vec<_>>().await.unwrap_err(),
            Error::Provider("reader failed".into())
        );
    }
}

#[tokio::test]
async fn valid_sse_with_invalid_tool_json_fails() {
    for arguments in ["{\"text\":", "[]", "null", "7"] {
        let data = format!(
            "data: {}\n\n",
            json!({
                "type":"response.completed", "response":{"output":[{
                    "type":"function_call", "call_id":"one", "name":"echo", "arguments":arguments,
                }]},
            })
        );
        assert!(
            parse(bytes(data.as_bytes()), openai::Response::default())
                .try_collect::<Vec<_>>()
                .await
                .is_err(),
            "{arguments}"
        );
        let events = [
            json!({"type":"message_start","message":{"role":"assistant","content":[]}}),
            json!({"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"one","name":"echo","input":{}}}),
            json!({"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":arguments}}),
            json!({"type":"content_block_stop","index":0}),
            json!({"type":"message_delta","delta":{"stop_reason":"tool_use"}}),
            json!({"type":"message_stop"}),
        ];
        let data: String = events
            .iter()
            .map(|event| format!("data: {event}\n\n"))
            .collect();
        assert!(
            parse(bytes(data.as_bytes()), anthropic::Response::default())
                .try_collect::<Vec<_>>()
                .await
                .is_err(),
            "{arguments}"
        );
    }
}

#[test]
fn neutral_history_serializes_without_replay() {
    let history = vec![
        Message::User { text: "go".into() },
        Message::Assistant {
            text: "working".into(),
            replay: vec![],
            tool_calls: vec![crate::ToolCall {
                call_id: "one".into(),
                name: "echo".into(),
                arguments: json!({"text":"hello"}),
            }],
        },
        Message::ToolResult {
            call_id: "one".into(),
            name: "echo".into(),
            output: "first".into(),
            is_error: false,
        },
        Message::ToolResult {
            call_id: "two".into(),
            name: "echo".into(),
            output: "second".into(),
            is_error: true,
        },
    ];
    let request = ProviderRequest {
        model: "fixture".into(),
        instructions: String::new(),
        history,
        tools: vec![],
    };
    let openai = openai().request(request.clone()).unwrap();
    let body: Value = serde_json::from_slice(openai.body().unwrap().as_bytes().unwrap()).unwrap();
    assert_eq!(body["input"][2]["arguments"], "{\"text\":\"hello\"}");
    let anthropic = anthropic().request(request).unwrap();
    let body: Value =
        serde_json::from_slice(anthropic.body().unwrap().as_bytes().unwrap()).unwrap();
    assert_eq!(
        body["messages"][1]["content"][1]["input"],
        json!({"text":"hello"})
    );
    assert_eq!(body["messages"][2]["content"].as_array().unwrap().len(), 2);
    assert_eq!(body["messages"][2]["content"][1]["is_error"], true);
}

#[tokio::test]
async fn local_http_provider_streams() {
    exercise_tcp(Kind::OpenAI, false).await;
    exercise_tcp(Kind::Anthropic, false).await;
}

#[tokio::test]
async fn local_http_cancellation_closes_connection() {
    exercise_tcp(Kind::OpenAI, true).await;
    exercise_tcp(Kind::Anthropic, true).await;
}

async fn exercise_tcp(kind: Kind, cancel: bool) {
    let listener =
        TcpListener::bind("127.0.0.1:0").expect("real TCP conformance requires local binding");
    let address = listener.local_addr().unwrap();
    listener.set_nonblocking(true).unwrap();
    let server = tokio::task::spawn_blocking(move || {
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        let (mut connection, _) = loop {
            match listener.accept() {
                Ok(connection) => break connection,
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        && std::time::Instant::now() < deadline =>
                {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("fixture could not accept a request: {error}"),
            }
        };
        connection.set_nonblocking(false).unwrap();
        connection
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut reader = BufReader::new(connection.try_clone().unwrap());
        let mut length = 0;
        loop {
            let mut line = String::new();
            assert_ne!(reader.read_line(&mut line).unwrap(), 0);
            if line == "\r\n" {
                break;
            }
            if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = value.trim().parse::<usize>().unwrap();
            }
        }
        assert!(length <= 1_048_576);
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        let request: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(request["stream"], true);
        let data = match kind {
            Kind::OpenAI => OPENAI_TEXT,
            Kind::Anthropic => ANTHROPIC_TEXT,
        };
        write!(connection, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n{:x}\r\n{data}\r\n", data.len()).unwrap();
        connection.flush().unwrap();
        // No terminal HTTP chunk. Completing or canceling must drop the body.
        let mut byte = [0];
        assert_eq!(reader.read(&mut byte).unwrap(), 0);
    });
    let cancellation = CancellationToken::new();
    let options = RunOptions {
        cancellation: cancellation.clone(),
        ..RunOptions::default()
    };
    let mut callback = |event: crate::Event| {
        if cancel && matches!(event.kind, EventKind::TextDelta { .. }) {
            cancellation.cancel();
        }
        ready(Ok(()))
    };
    let result = match kind {
        Kind::OpenAI => {
            let mut provider = openai();
            provider.endpoint = format!("http://{address}/responses");
            Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                tools: vec![],
                provider,
            })
            .unwrap()
            .run_with_events("go", options, &mut callback)
            .await
        }
        Kind::Anthropic => {
            let mut provider = anthropic();
            provider.endpoint = format!("http://{address}/messages");
            Agent::new(AgentConfig {
                model: "fixture".into(),
                instructions: String::new(),
                tools: vec![],
                provider,
            })
            .unwrap()
            .run_with_events("go", options, &mut callback)
            .await
        }
    };
    server.await.unwrap();
    if cancel {
        assert_eq!(result.unwrap_err(), Error::Cancelled);
    } else {
        assert_eq!(result.unwrap().text, "done\ncafé");
    }
}
