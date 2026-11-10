use futures_util::stream;
use serde_json::{json, Value};
use std::{sync::Arc, time::Duration};
use tiny_agent::{
    run_rpc, Agent, AgentConfig, CancellationToken, Error, Provider, ProviderEvent,
    ProviderRequest, ProviderStream, ProviderTurn, Result, Tool, ToolCall, ToolDefinition,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::Notify,
    time::{sleep, timeout},
};

struct ToolProvider;

impl Provider for ToolProvider {
    async fn stream(&self, _: ProviderRequest) -> Result<ProviderStream> {
        Ok(Box::pin(stream::iter([Ok(ProviderEvent::Turn(
            ProviderTurn {
                text: String::new(),
                replay: vec![],
                output: vec![],
                tool_calls: vec![ToolCall {
                    call_id: "one".into(),
                    name: "wait".into(),
                    arguments: json!({}),
                }],
            },
        ))])))
    }
}

#[tokio::test]
async fn slow_cooperative_cleanup_keeps_output_open_for_paired_events() {
    let started = Arc::new(Notify::new());
    let cancelled = Arc::new(Notify::new());
    let release = Arc::new(Notify::new());
    let (entered, stopping, finish) = (started.clone(), cancelled.clone(), release.clone());
    let tool = Tool::new(
        ToolDefinition {
            name: "wait".into(),
            description: "Wait for cancellation and cleanup".into(),
            parameters: json!({"type":"object"}),
        },
        move |_, token| {
            let (entered, stopping, finish) = (entered.clone(), stopping.clone(), finish.clone());
            async move {
                entered.notify_one();
                token.cancelled().await;
                stopping.notify_one();
                finish.notified().await;
                Err(Error::Cancelled)
            }
        },
    )
    .unwrap();
    let agent = Arc::new(
        Agent::new(AgentConfig {
            model: "fixture".into(),
            instructions: String::new(),
            provider: ToolProvider,
            tools: vec![tool],
        })
        .unwrap(),
    );
    let (mut input, reader) = tokio::io::duplex(4096);
    let (output, receiver) = tokio::io::duplex(4096);
    let task = tokio::spawn(run_rpc(agent, reader, output, CancellationToken::new()));
    let mut lines = BufReader::new(receiver).lines();
    input
        .write_all(b"{\"type\":\"prompt\",\"message\":\"wait\"}\n")
        .await
        .unwrap();
    timeout(Duration::from_secs(1), started.notified())
        .await
        .unwrap();
    for expected in ["response", "agent_start", "tool_execution_start"] {
        let line = timeout(Duration::from_secs(1), lines.next_line())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&line).unwrap()["type"],
            expected
        );
    }
    input
        .write_all(b"{\"id\":\"abort\",\"type\":\"abort\"}\n")
        .await
        .unwrap();
    timeout(Duration::from_secs(1), cancelled.notified())
        .await
        .unwrap();
    let line = timeout(Duration::from_secs(1), lines.next_line())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&line).unwrap()["data"]["accepted"],
        true
    );
    sleep(Duration::from_millis(1100)).await;
    assert!(
        !task.is_finished(),
        "driver stopped before cooperative cleanup"
    );
    input
        .write_all(b"{\"type\":\"get_state\"}\n")
        .await
        .unwrap();
    let line = timeout(Duration::from_secs(1), lines.next_line())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&line).unwrap()["data"]["status"],
        "running"
    );
    release.notify_one();
    for expected in ["tool_execution_end", "agent_end"] {
        let line = timeout(Duration::from_secs(1), lines.next_line())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let event: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(event["type"], expected);
        if expected == "tool_execution_end" {
            assert_eq!(event["toolCallId"], "one");
            assert_eq!(event["isError"], true);
        } else {
            assert_eq!(event["status"], "aborted");
        }
    }
    drop(input);
    timeout(Duration::from_secs(1), task)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(lines.next_line().await.unwrap().is_none());
}
