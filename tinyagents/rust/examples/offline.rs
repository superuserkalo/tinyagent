use futures_util::stream;
use serde::Deserialize;
use serde_json::json;
use tiny_agent::{
    Agent, AgentConfig, Error, Message, Provider, ProviderEvent, ProviderRequest, ProviderStream,
    ProviderTurn, Result, RunOptions, Tool, ToolCall, ToolDefinition,
};

// A local provider makes the SDK example runnable without credentials or network.
struct EchoProvider;

impl Provider for EchoProvider {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        let turn = match request.history.last() {
            Some(Message::ToolResult { output, .. }) => ProviderTurn {
                text: output.clone(),
                tool_calls: vec![],
                replay: vec![],
                output: vec![],
            },
            _ => ProviderTurn {
                text: String::new(),
                tool_calls: vec![ToolCall {
                    call_id: "echo".into(),
                    name: "echo".into(),
                    arguments: json!({"text":"hello"}),
                }],
                replay: vec![],
                output: vec![],
            },
        };
        Ok(Box::pin(stream::iter([Ok(ProviderEvent::Turn(turn))])))
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    #[derive(Deserialize)]
    struct Arguments {
        text: String,
    }
    let echo = Tool::new(
        ToolDefinition {
            name: "echo".into(),
            description: "Return text".into(),
            parameters: json!({
                "type":"object", "properties":{"text":{"type":"string"}},
                "required":["text"], "additionalProperties":false,
            }),
        },
        |arguments, _cancellation| async move {
            let arguments: Arguments = serde_json::from_value(arguments)
                .map_err(|error| Error::Tool(error.to_string()))?;
            Ok(arguments.text)
        },
    )?;
    let agent = Agent::new(AgentConfig {
        model: "fixture".into(),
        instructions: "Use echo".into(),
        provider: EchoProvider,
        tools: vec![echo],
    })?;
    let result = agent.run("Echo hello", RunOptions::default()).await?;
    println!("{}", result.text);
    Ok(())
}
