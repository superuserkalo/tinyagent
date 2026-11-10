//! Terminal entrypoint backed by the same agent used by SDK and RPC callers.

use crate::{
    bash_tool, denied_reason,
    lines::{LineError, Lines},
    run_rpc, serve, Agent, AgentConfig, AnthropicProvider, CancellationToken, Error, EventKind,
    HttpOptions, OpenAIProvider, Provider, ProviderRequest, ProviderStream, Result, RunOptions,
};
use std::{env, net::IpAddr, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt},
    sync::Mutex,
};

const INSTRUCTIONS: &str = "You are TinyAgent, a coding agent in the current directory.\nInspect before changing files. Use bash for shell and file work. Each call is a fresh process.";

enum ConfiguredProvider {
    OpenAI(OpenAIProvider),
    Anthropic(AnthropicProvider),
}

impl Provider for ConfiguredProvider {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        match self {
            Self::OpenAI(provider) => provider.stream(request).await,
            Self::Anthropic(provider) => provider.stream(request).await,
        }
    }
}

pub async fn run<R, W, D>(
    args: &[String],
    input: R,
    output: W,
    diagnostics: D,
    cancellation: CancellationToken,
) -> Result<()>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
    D: AsyncWrite + Unpin + Send,
{
    if args == ["--self-test"] {
        if denied_reason("printf 'hello\\n'").is_some() {
            return Err(Error::Configuration("Safe command was denied".into()));
        }
        for command in [
            "rm -rf /",
            "r\"\"m -rf /",
            "sudo echo hi",
            "git reset --hard HEAD",
            "find . -delete",
            "mkfs.ext4 /dev/sda",
        ] {
            if denied_reason(command).is_none() {
                return Err(Error::Configuration(format!(
                    "Unsafe command was allowed: {command}"
                )));
            }
        }
        return write(&Mutex::new(output), "self-test passed\n", &cancellation).await;
    }
    let rpc = args == ["--mode", "rpc"];
    let http = args.first().is_some_and(|argument| argument == "serve");
    if !args.is_empty() && !rpc && !http {
        return Err(Error::Configuration(
            "usage: tiny-agent [--self-test | --mode rpc | serve [--host IP] [--port PORT] [--token TOKEN] [--allow-remote]]".into(),
        ));
    }
    let http_options = if http {
        Some(parse_http_options(&args[1..])?)
    } else {
        None
    };
    let (provider, default_model) = match env::var("TINYAGENT_PROVIDER").as_deref().unwrap_or("") {
        "" | "openai" => (
            ConfiguredProvider::OpenAI(OpenAIProvider::new()?),
            "gpt-5.6",
        ),
        "anthropic" => (
            ConfiguredProvider::Anthropic(AnthropicProvider::new()?),
            "claude-sonnet-4-6",
        ),
        kind => return Err(Error::Configuration(format!("Unknown provider: {kind}"))),
    };
    let agent = Arc::new(Agent::new(AgentConfig {
        model: env::var("TINYAGENT_MODEL")
            .ok()
            .filter(|model| !model.is_empty())
            .unwrap_or_else(|| default_model.into()),
        instructions: INSTRUCTIONS.into(),
        provider,
        tools: vec![bash_tool()?],
    })?);
    if rpc {
        return run_rpc(agent, input, output, cancellation).await;
    }
    if let Some(options) = http_options {
        let server = serve(agent, options).await?;
        let startup = write(
            &Mutex::new(diagnostics),
            &format!("TinyAgent listening on {}\n", server.url()),
            &cancellation,
        )
        .await;
        if let Err(error) = startup {
            server.close().await?;
            return Err(error);
        }
        return tokio::select! {
            _ = cancellation.cancelled() => server.close().await,
            result = server.wait() => result,
        };
    }
    interactive(&agent, input, output, diagnostics, cancellation).await
}

fn parse_http_options(args: &[String]) -> Result<HttpOptions> {
    let mut options = HttpOptions {
        token: env::var("TINYAGENT_TOKEN").unwrap_or_default(),
        ..HttpOptions::default()
    };
    let mut args = args.iter();
    while let Some(flag) = args.next() {
        if flag == "--allow-remote" {
            options.allow_remote = true;
            continue;
        }
        if !matches!(flag.as_str(), "--host" | "--port" | "--token") {
            return Err(Error::Configuration(format!(
                "Unknown serve option: {flag}"
            )));
        }
        let value = args
            .next()
            .ok_or_else(|| Error::Configuration(format!("{flag} requires a value")))?;
        match flag.as_str() {
            "--host" => {
                let host: IpAddr = if value == "localhost" {
                    "127.0.0.1".parse().unwrap()
                } else {
                    value.parse().map_err(|_| {
                        Error::Configuration("Host must be an IP address or localhost".into())
                    })?
                };
                options.address.set_ip(host);
            }
            "--port" => options.address.set_port(value.parse().map_err(|_| {
                Error::Configuration("Port must be an integer from 0 to 65535".into())
            })?),
            "--token" => options.token = value.clone(),
            _ => unreachable!("flag checked above"),
        }
    }
    Ok(options)
}

async fn interactive<P, R, W, D>(
    agent: &Agent<P>,
    input: R,
    output: W,
    diagnostics: D,
    cancellation: CancellationToken,
) -> Result<()>
where
    P: Provider,
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send,
    D: AsyncWrite + Unpin + Send,
{
    let output = Arc::new(Mutex::new(output));
    let diagnostics = Arc::new(Mutex::new(diagnostics));
    let mut lines = Lines::new(input);
    let mut history = Vec::new();
    write(
        &output,
        "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n",
        &cancellation,
    )
    .await?;
    loop {
        if cancellation.is_cancelled() {
            return Ok(());
        }
        write(&output, "\n> ", &cancellation).await?;
        let line = tokio::select! {
            _ = cancellation.cancelled() => return Ok(()),
            line = lines.next() => line,
        };
        let line = match line {
            Ok(Some(line)) => line,
            Ok(None) => return Ok(()),
            Err(LineError::Io(error)) => return Err(io_error(error)),
            Err(LineError::TooLarge) => {
                return Err(Error::Consumer("Input exceeds 1048576 UTF-8 bytes".into()))
            }
            Err(LineError::InvalidUtf8) => {
                return Err(Error::Consumer("Input is not valid UTF-8".into()))
            }
        };
        let prompt = line.trim();
        if prompt.is_empty() {
            continue;
        }
        let mut streamed = false;
        let result = agent
            .run_with_events(
                prompt,
                RunOptions {
                    history: history.clone(),
                    cancellation: cancellation.clone(),
                    ..RunOptions::default()
                },
                |event| {
                    let output = output.clone();
                    let diagnostics = diagnostics.clone();
                    let cancellation = cancellation.clone();
                    let message = match event.kind {
                        EventKind::TextDelta { delta } => {
                            streamed = true;
                            Some((true, delta))
                        }
                        EventKind::ToolExecutionStart { arguments, .. } => arguments["command"]
                            .as_str()
                            .map(|command| (false, format!("\n$ {command}\n"))),
                        EventKind::ToolExecutionEnd { output, .. } => {
                            streamed = false;
                            Some((false, format!("{output}\n")))
                        }
                        _ => None,
                    };
                    async move {
                        match message {
                            Some((true, text)) => write(&output, &text, &cancellation).await,
                            Some((false, text)) => write(&diagnostics, &text, &cancellation).await,
                            None => Ok(()),
                        }
                    }
                },
            )
            .await;
        match result {
            Err(error @ Error::Consumer(_)) => return Err(error),
            _ if cancellation.is_cancelled() => return Ok(()),
            Err(error) => write(&diagnostics, &format!("error: {error}\n"), &cancellation).await?,
            Ok(result) => {
                history = result.history;
                write(
                    &output,
                    &if streamed {
                        "\n".into()
                    } else {
                        format!("{}\n", result.text)
                    },
                    &cancellation,
                )
                .await?;
            }
        }
    }
}

async fn write<W: AsyncWrite + Unpin>(
    output: &Mutex<W>,
    text: &str,
    cancellation: &CancellationToken,
) -> Result<()> {
    let write = async {
        let mut output = output.lock().await;
        output.write_all(text.as_bytes()).await.map_err(io_error)?;
        output.flush().await.map_err(io_error)
    };
    tokio::pin!(write);
    tokio::select! {
        result = &mut write => result,
        _ = cancellation.cancelled() => {
            tokio::time::timeout(Duration::from_secs(1), &mut write).await
                .map_err(|_| Error::Consumer("Console output did not drain after cancellation".into()))?
        }
    }
}

fn io_error(error: std::io::Error) -> Error {
    Error::Consumer(error.to_string())
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod cancellation_tests;
