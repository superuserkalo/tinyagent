use futures_util::{stream, StreamExt};
use serde_json::json;
use std::{sync::Arc, time::Duration};
use tiny_agent::{
    bash_tool, run_rpc, Agent, AgentConfig, CancellationToken, Error, Message, Provider,
    ProviderEvent, ProviderRequest, ProviderStream, ProviderTurn, Result, ToolCall,
    MAX_JSONL_LINE_BYTES,
};

struct FixtureProvider;

impl Provider for FixtureProvider {
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
        let delta = match prompt {
            "wait" | "delayed" => "waiting",
            "fail" => "partial",
            _ => "done\nfixture",
        };
        if prompt == "wait" {
            return Ok(Box::pin(
                stream::once(async { Ok(ProviderEvent::TextDelta("waiting".into())) })
                    .chain(stream::pending()),
            ));
        }
        if prompt == "fail" {
            return Ok(Box::pin(stream::iter([
                Ok(ProviderEvent::TextDelta(delta.into())),
                Err(Error::Provider("fixture failure".into())),
            ])));
        }
        if prompt == "oversized" {
            return Ok(Box::pin(stream::iter([Ok(ProviderEvent::TextDelta(
                "x".repeat(MAX_JSONL_LINE_BYTES),
            ))])));
        }
        let tools = if prompt == "wait_tool"
            && !matches!(request.history.last(), Some(Message::ToolResult { .. }))
        {
            vec![ToolCall {
                call_id: "bash-1".into(),
                name: "bash".into(),
                arguments: json!({"command":"sleep 3 &"}),
            }]
        } else {
            vec![]
        };
        let turn = ProviderTurn {
            text: if tools.is_empty() {
                "done\nfixture".into()
            } else {
                String::new()
            },
            tool_calls: tools,
            replay: vec![],
            output: vec![],
        };
        let delayed = prompt == "delayed";
        let delta = delta.to_owned();
        Ok(Box::pin(
            stream::once(async move { Ok(ProviderEvent::TextDelta(delta)) }).chain(stream::once(
                async move {
                    if delayed {
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                    Ok(ProviderEvent::Turn(turn))
                },
            )),
        ))
    }
}

fn main() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let result = runtime.block_on(async {
        let cancellation = CancellationToken::new();
        #[cfg(unix)]
        {
            use tokio::signal::unix::{signal, SignalKind};
            let mut interrupt = signal(SignalKind::interrupt()).unwrap();
            let token = cancellation.clone();
            tokio::spawn(async move {
                interrupt.recv().await;
                token.cancel();
            });
        }
        let agent = Agent::new(AgentConfig {
            model: "fixture".into(),
            instructions: String::new(),
            provider: FixtureProvider,
            tools: vec![bash_tool()?],
        })?;
        run_rpc(
            Arc::new(agent),
            tokio::io::stdin(),
            tokio::io::stdout(),
            cancellation,
        )
        .await
    });
    runtime.shutdown_background();
    result
}
