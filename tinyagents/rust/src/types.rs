use futures_util::stream::BoxStream;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{fmt, future::Future};
use tokio_util::sync::CancellationToken;

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_JSONL_LINE_BYTES: usize = 1_048_576;
pub const MAX_HTTP_BODY_BYTES: usize = 1_048_576;
pub const MAX_QUEUED_OUTPUT_BYTES: usize = 4_194_304;
pub const MAX_TOOL_OUTPUT_BYTES: usize = 131_072;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    Cancelled,
    Configuration(String),
    Provider(String),
    Tool(String),
    Consumer(String),
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Cancelled => formatter.write_str("Run cancelled"),
            Self::Configuration(message)
            | Self::Provider(message)
            | Self::Tool(message)
            | Self::Consumer(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(
    tag = "role",
    rename_all = "snake_case",
    rename_all_fields = "camelCase"
)]
pub enum Message {
    User {
        text: String,
    },
    Assistant {
        text: String,
        tool_calls: Vec<ToolCall>,
        replay: Vec<Value>,
    },
    ToolResult {
        call_id: String,
        name: String,
        output: String,
        is_error: bool,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ToolCall {
    pub call_id: String,
    pub name: String,
    pub arguments: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}

#[derive(Debug, Clone)]
pub struct ProviderRequest {
    pub model: String,
    pub instructions: String,
    pub history: Vec<Message>,
    pub tools: Vec<ToolDefinition>,
}

#[derive(Debug, Clone)]
pub struct ProviderTurn {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub replay: Vec<Value>,
    pub output: Vec<Value>,
}

#[derive(Debug)]
pub enum ProviderEvent {
    TextDelta(String),
    Turn(ProviderTurn),
}

pub type ProviderStream = BoxStream<'static, Result<ProviderEvent>>;

/// Providers yield deltas followed by one complete Turn. Dropping their request
/// future or stream must release its I/O; this is how the core cancels providers.
pub trait Provider: Send + Sync {
    fn stream(
        &self,
        request: ProviderRequest,
    ) -> impl Future<Output = Result<ProviderStream>> + Send;
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunResult {
    pub text: String,
    pub history: Vec<Message>,
    pub output: Vec<Value>,
}

#[derive(Debug, Clone, Default)]
pub struct RunOptions {
    pub run_id: Option<String>,
    pub history: Vec<Message>,
    pub cancellation: CancellationToken,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Event {
    pub run_id: String,
    pub sequence: u64,
    #[serde(flatten)]
    pub kind: EventKind,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(
    tag = "type",
    rename_all = "snake_case",
    rename_all_fields = "camelCase"
)]
pub enum EventKind {
    AgentStart,
    TextDelta {
        delta: String,
    },
    ToolExecutionStart {
        tool_call_id: String,
        tool_name: String,
        arguments: Value,
    },
    ToolExecutionEnd {
        tool_call_id: String,
        tool_name: String,
        output: String,
        is_error: bool,
    },
    AgentEnd {
        #[serde(flatten)]
        outcome: RunOutcome,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum RunOutcome {
    Completed { text: String },
    Failed { error: RunError },
    Aborted,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}
