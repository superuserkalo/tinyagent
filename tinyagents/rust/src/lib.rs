//! A native, in-memory model and tool loop. Adapters share this core.

mod agent;
mod bash;
pub mod cli;
mod http;
mod lines;
mod providers;
mod rpc;
mod session;
mod tool;
mod types;

pub use agent::{Agent, AgentConfig};
pub use bash::{bash_tool, denied_reason, run_bash};
pub use http::{serve, HttpOptions, HttpServer};
pub use providers::{AnthropicProvider, OpenAIProvider};
pub use rpc::run_rpc;
pub use tokio_util::sync::CancellationToken;
pub use tool::Tool;
pub use types::*;
