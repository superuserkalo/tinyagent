use futures_util::{stream, Stream, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    future::ready,
    pin::Pin,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
};
use tiny_agent::{
    Agent, AgentConfig, CancellationToken, Error, EventKind, Message, Provider, ProviderEvent,
    ProviderRequest, ProviderStream, ProviderTurn, Result, RunOptions, RunOutcome, Tool, ToolCall,
    ToolDefinition, MAX_TOOL_OUTPUT_BYTES,
};
use tokio::sync::Notify;

fn definition(schema: Value) -> ToolDefinition {
    ToolDefinition {
        name: "echo".into(),
        description: "Echo text".into(),
        parameters: schema,
    }
}

fn complete(text: &str) -> Result<ProviderEvent> {
    Ok(ProviderEvent::Turn(ProviderTurn {
        text: text.into(),
        tool_calls: vec![],
        replay: vec![],
        output: vec![],
    }))
}

struct Scripted {
    steps: Mutex<VecDeque<Vec<Result<ProviderEvent>>>>,
    requests: Arc<Mutex<Vec<ProviderRequest>>>,
}

impl Provider for Scripted {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        self.requests.lock().unwrap().push(request);
        let events = self
            .steps
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected provider request");
        Ok(stream::iter(events).boxed())
    }
}

fn agent(provider: impl Provider, tools: Vec<Tool>) -> Agent<impl Provider> {
    Agent::new(AgentConfig {
        model: "fixture".into(),
        instructions: String::new(),
        provider,
        tools,
    })
    .unwrap()
}

#[tokio::test]
async fn shared_tool_argument_cases() {
    #[derive(Deserialize)]
    struct Fixture {
        cases: Vec<Case>,
    }
    #[derive(Deserialize)]
    struct Case {
        name: String,
        schema: Value,
        valid: Vec<Value>,
        invalid: Vec<Value>,
    }
    let fixture: Fixture =
        serde_json::from_str(include_str!("../conformance/tool_arguments.json")).unwrap();
    for case in fixture.cases {
        let handled = Arc::new(Mutex::new(Vec::new()));
        let calls = handled.clone();
        let tool = Tool::new(definition(case.schema), move |arguments, _| {
            calls.lock().unwrap().push(arguments);
            ready(Ok("ok".into()))
        })
        .unwrap_or_else(|error| panic!("{}: {error}", case.name));
        for valid in &case.valid {
            assert_eq!(
                tool.execute(valid.clone(), CancellationToken::new())
                    .await
                    .unwrap(),
                "ok",
                "{}",
                case.name
            );
        }
        for invalid in &case.invalid {
            assert!(
                tool.execute(invalid.clone(), CancellationToken::new())
                    .await
                    .is_err(),
                "{}: {invalid}",
                case.name
            );
        }
        assert_eq!(*handled.lock().unwrap(), case.valid, "{}", case.name);
    }
}

#[test]
fn schemas_fail_at_construction() {
    for schema in [
        json!({"type":"invalid"}),
        json!({"$async":true}),
        json!({"$ref":"https://example.invalid/schema"}),
        json!({"$ref":"file:///unreadable"}),
    ] {
        assert!(Tool::new(definition(schema), |_, _| ready(Ok(String::new()))).is_err());
    }
}

#[tokio::test]
async fn ordered_tools_recover_and_bound_output() {
    let requests = Arc::new(Mutex::new(Vec::new()));
    let handled = Arc::new(Mutex::new(Vec::new()));
    let seen = handled.clone();
    let tool = Tool::new(
        definition(
            json!({"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}),
        ),
        move |arguments, _| {
            let text = arguments["text"].as_str().unwrap();
            seen.lock().unwrap().push(text.to_owned());
            ready(match text {
                "fail" => Err(Error::Tool("recoverable failure".into())),
                "large" => Ok("x".repeat(MAX_TOOL_OUTPUT_BYTES + 1)),
                _ => Ok(text.to_owned()),
            })
        },
    )
    .unwrap();
    let calls = [
        json!({"text":7}),
        json!({"text":"fail"}),
        json!({"text":"large"}),
        json!({"text":"ok"}),
    ]
    .into_iter()
    .enumerate()
    .map(|(index, arguments)| ToolCall {
        call_id: index.to_string(),
        name: "echo".into(),
        arguments,
    })
    .collect();
    let provider = Scripted {
        requests: requests.clone(),
        steps: Mutex::new(VecDeque::from([
            vec![
                Ok(ProviderEvent::TextDelta("working".into())),
                Ok(ProviderEvent::Turn(ProviderTurn {
                    text: "working".into(),
                    tool_calls: calls,
                    replay: vec![json!({"opaque":true})],
                    output: vec![],
                })),
            ],
            vec![
                Ok(ProviderEvent::TextDelta("done".into())),
                complete("done"),
            ],
        ])),
    };
    let mut events = Vec::new();
    let result = agent(provider, vec![tool])
        .run_with_events(
            "go",
            RunOptions {
                run_id: Some("fixture".into()),
                ..RunOptions::default()
            },
            |event| {
                events.push(event);
                ready(Ok(()))
            },
        )
        .await
        .unwrap();
    assert_eq!(result.text, "done");
    assert_eq!(result.history.len(), 7);
    assert_eq!(*handled.lock().unwrap(), ["fail", "large", "ok"]);
    assert_eq!(events.len(), 12);
    for (index, event) in events.iter().enumerate() {
        assert_eq!(event.run_id, "fixture");
        assert_eq!(event.sequence, index as u64 + 1);
        if let EventKind::ToolExecutionEnd {
            output,
            is_error,
            tool_call_id,
            ..
        } = &event.kind
        {
            assert_eq!(*is_error, tool_call_id != "3");
            assert!(output.len() <= MAX_TOOL_OUTPUT_BYTES);
        }
    }
    assert!(matches!(
        events.first().unwrap().kind,
        EventKind::AgentStart
    ));
    assert!(matches!(
        events.last().unwrap().kind,
        EventKind::AgentEnd {
            outcome: RunOutcome::Completed { .. }
        }
    ));
    let requests = requests.lock().unwrap();
    assert_eq!(requests[1].history.len(), 6);
    assert!(
        matches!(&requests[1].history[1], Message::Assistant { replay, .. } if replay == &vec![json!({"opaque":true})])
    );
    let wire = serde_json::to_value(&events[3]).unwrap();
    assert_eq!(wire["type"], "tool_execution_end");
    assert_eq!(wire["toolCallId"], "0");
    assert_eq!(wire["isError"], true);
}

#[tokio::test]
async fn failure_keeps_prior_history() {
    let history = vec![Message::User {
        text: "keep".into(),
    }];
    let provider = Scripted {
        requests: Arc::new(Mutex::new(Vec::new())),
        steps: Mutex::new(VecDeque::from([vec![
            Ok(ProviderEvent::TextDelta("partial".into())),
            Err(Error::Provider("failed".into())),
        ]])),
    };
    let mut events = Vec::new();
    let error = agent(provider, vec![])
        .run_with_events(
            "go",
            RunOptions {
                history: history.clone(),
                ..RunOptions::default()
            },
            |event| {
                events.push(event);
                ready(Ok(()))
            },
        )
        .await
        .unwrap_err();
    assert_eq!(error, Error::Provider("failed".into()));
    assert_eq!(
        history,
        [Message::User {
            text: "keep".into()
        }]
    );
    assert!(matches!(
        events.last().unwrap().kind,
        EventKind::AgentEnd {
            outcome: RunOutcome::Failed { .. }
        }
    ));
}

struct GuardedStream {
    inner: ProviderStream,
    dropped: Arc<AtomicBool>,
}

impl Stream for GuardedStream {
    type Item = Result<ProviderEvent>;
    fn poll_next(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.get_mut().inner.as_mut().poll_next(context)
    }
}

impl Drop for GuardedStream {
    fn drop(&mut self) {
        self.dropped.store(true, Ordering::SeqCst);
    }
}

struct GuardedProvider {
    events: Mutex<Option<Vec<Result<ProviderEvent>>>>,
    dropped: Arc<AtomicBool>,
    started: Arc<Notify>,
}

impl Provider for GuardedProvider {
    async fn stream(&self, _: ProviderRequest) -> Result<ProviderStream> {
        let events = self.events.lock().unwrap().take().unwrap();
        self.started.notify_one();
        Ok(GuardedStream {
            inner: stream::iter(events).chain(stream::pending()).boxed(),
            dropped: self.dropped.clone(),
        }
        .boxed())
    }
}

#[tokio::test]
async fn provider_stream_is_dropped_on_completion_and_consumer_failure() {
    for consumer_failure in [false, true] {
        let dropped = Arc::new(AtomicBool::new(false));
        let provider = GuardedProvider {
            dropped: dropped.clone(),
            started: Arc::new(Notify::new()),
            events: Mutex::new(Some(if consumer_failure {
                vec![Ok(ProviderEvent::TextDelta("one".into()))]
            } else {
                vec![complete("done")]
            })),
        };
        let mut events = Vec::new();
        let result = agent(provider, vec![])
            .run_with_events("go", RunOptions::default(), |event| {
                let fail = consumer_failure && matches!(event.kind, EventKind::TextDelta { .. });
                events.push(event);
                ready(if fail {
                    Err(Error::Consumer("writer closed".into()))
                } else {
                    Ok(())
                })
            })
            .await;
        assert_eq!(result.is_err(), consumer_failure);
        assert!(dropped.load(Ordering::SeqCst));
        if consumer_failure {
            assert_eq!(events.len(), 2, "attempted writes after consumer failure");
        }
    }
}

#[tokio::test]
async fn cancellation_drops_provider_stream() {
    let dropped = Arc::new(AtomicBool::new(false));
    let started = Arc::new(Notify::new());
    let provider = GuardedProvider {
        dropped: dropped.clone(),
        started: started.clone(),
        events: Mutex::new(Some(vec![])),
    };
    let agent = agent(provider, vec![]);
    let cancellation = CancellationToken::new();
    let mut events = Vec::new();
    let run = agent.run_with_events(
        "go",
        RunOptions {
            cancellation: cancellation.clone(),
            ..RunOptions::default()
        },
        |event| {
            events.push(event);
            ready(Ok(()))
        },
    );
    let (result, ()) = tokio::join!(run, async {
        started.notified().await;
        cancellation.cancel();
    });
    assert_eq!(result.unwrap_err(), Error::Cancelled);
    assert!(dropped.load(Ordering::SeqCst));
    assert!(matches!(
        events.last().unwrap().kind,
        EventKind::AgentEnd {
            outcome: RunOutcome::Aborted
        }
    ));
}

#[tokio::test]
async fn cancellation_pairs_tool_events() {
    let started = Arc::new(Notify::new());
    let entered = started.clone();
    let tool = Tool::new(
        definition(json!({"type":"object"})),
        move |_, cancellation| {
            entered.notify_one();
            async move {
                cancellation.cancelled().await;
                Err(Error::Cancelled)
            }
        },
    )
    .unwrap();
    let provider = Scripted {
        requests: Arc::new(Mutex::new(Vec::new())),
        steps: Mutex::new(VecDeque::from([vec![Ok(ProviderEvent::Turn(
            ProviderTurn {
                text: String::new(),
                replay: vec![],
                output: vec![],
                tool_calls: vec![ToolCall {
                    call_id: "one".into(),
                    name: "echo".into(),
                    arguments: json!({}),
                }],
            },
        ))]])),
    };
    let agent = agent(provider, vec![tool]);
    let cancellation = CancellationToken::new();
    let mut events = Vec::new();
    let run = agent.run_with_events(
        "go",
        RunOptions {
            cancellation: cancellation.clone(),
            ..RunOptions::default()
        },
        |event| {
            events.push(event);
            ready(Ok(()))
        },
    );
    let (result, ()) = tokio::join!(run, async {
        started.notified().await;
        cancellation.cancel();
    });
    assert_eq!(result.unwrap_err(), Error::Cancelled);
    assert_eq!(events.len(), 4);
    assert!(matches!(
        events[1].kind,
        EventKind::ToolExecutionStart { .. }
    ));
    assert!(matches!(
        events[2].kind,
        EventKind::ToolExecutionEnd { is_error: true, .. }
    ));
    assert!(matches!(
        events[3].kind,
        EventKind::AgentEnd {
            outcome: RunOutcome::Aborted
        }
    ));
}

#[tokio::test]
async fn malformed_or_missing_provider_turn_fails_before_tool_execution() {
    for events in [
        vec![],
        vec![Ok(ProviderEvent::Turn(ProviderTurn {
            text: String::new(),
            replay: vec![],
            output: vec![],
            tool_calls: vec![ToolCall {
                call_id: "one".into(),
                name: "echo".into(),
                arguments: json!("not an object"),
            }],
        }))],
    ] {
        let provider = Scripted {
            requests: Arc::new(Mutex::new(Vec::new())),
            steps: Mutex::new(VecDeque::from([events])),
        };
        let mut events = Vec::new();
        let result = agent(provider, vec![])
            .run_with_events("go", RunOptions::default(), |event| {
                events.push(event);
                ready(Ok(()))
            })
            .await;
        assert!(matches!(result, Err(Error::Provider(_))));
        assert_eq!(events.len(), 2);
    }
}
