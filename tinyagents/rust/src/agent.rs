use crate::{
    CancellationToken, Error, Event, EventKind, Message, Provider, ProviderEvent, ProviderRequest,
    Result, RunError, RunOptions, RunOutcome, RunResult, Tool, ToolDefinition,
    MAX_TOOL_OUTPUT_BYTES,
};
use futures_util::StreamExt;
use std::{
    collections::BTreeMap,
    future::{ready, Future},
};
use uuid::Uuid;

pub struct AgentConfig<P> {
    pub model: String,
    pub instructions: String,
    pub provider: P,
    pub tools: Vec<Tool>,
}

pub struct Agent<P> {
    model: String,
    instructions: String,
    provider: P,
    tools: BTreeMap<String, Tool>,
    definitions: Vec<ToolDefinition>,
}

impl<P: Provider> Agent<P> {
    pub fn new(config: AgentConfig<P>) -> Result<Self> {
        if config.model.is_empty() {
            return Err(Error::Configuration("Model is required".into()));
        }
        let mut tools = BTreeMap::new();
        let mut definitions = Vec::new();
        for tool in config.tools {
            let definition = tool.definition();
            if tools.insert(definition.name.clone(), tool).is_some() {
                return Err(Error::Configuration(format!(
                    "Duplicate tool: {}",
                    definition.name
                )));
            }
            definitions.push(definition);
        }
        Ok(Self {
            model: config.model,
            instructions: config.instructions,
            provider: config.provider,
            tools,
            definitions,
        })
    }

    pub async fn run(&self, prompt: &str, options: RunOptions) -> Result<RunResult> {
        self.run_with_events(prompt, options, |_| ready(Ok(())))
            .await
    }

    /// The callback applies backpressure. Its error stops the run without
    /// attempting more writes. Only a successful return contains new history.
    pub async fn run_with_events<F, Fut>(
        &self,
        prompt: &str,
        options: RunOptions,
        on_event: F,
    ) -> Result<RunResult>
    where
        F: FnMut(Event) -> Fut + Send,
        Fut: Future<Output = Result<()>> + Send,
    {
        let cancellation = options.cancellation.child_token();
        let _cancel_on_drop = cancellation.clone().drop_guard();
        let mut events = Events {
            run_id: options
                .run_id
                .unwrap_or_else(|| format!("r_{}", Uuid::new_v4().simple())),
            sequence: 0,
            failed: false,
            callback: on_event,
        };
        events.emit(EventKind::AgentStart).await?;
        let mut result = self
            .run_inner(prompt, options.history, &cancellation, &mut events)
            .await;
        if events.failed {
            return result;
        }
        if cancellation.is_cancelled() {
            result = Err(Error::Cancelled);
        }
        let outcome = match &result {
            Ok(result) => RunOutcome::Completed {
                text: result.text.clone(),
            },
            Err(Error::Cancelled) => RunOutcome::Aborted,
            Err(error) => RunOutcome::Failed {
                error: RunError {
                    code: "provider_error".into(),
                    message: bounded_observation(error.to_string()),
                    retryable: true,
                },
            },
        };
        events.emit(EventKind::AgentEnd { outcome }).await?;
        if cancellation.is_cancelled() {
            Err(Error::Cancelled)
        } else {
            result
        }
    }

    async fn run_inner<F, Fut>(
        &self,
        prompt: &str,
        mut history: Vec<Message>,
        cancellation: &CancellationToken,
        events: &mut Events<F>,
    ) -> Result<RunResult>
    where
        F: FnMut(Event) -> Fut + Send,
        Fut: Future<Output = Result<()>> + Send,
    {
        history.push(Message::User {
            text: prompt.into(),
        });
        loop {
            let request = ProviderRequest {
                model: self.model.clone(),
                instructions: self.instructions.clone(),
                history: history.clone(),
                tools: self.definitions.clone(),
            };
            let mut stream = tokio::select! {
                biased;
                _ = cancellation.cancelled() => return Err(Error::Cancelled),
                stream = self.provider.stream(request) => stream?,
            };
            let turn = loop {
                let event = tokio::select! {
                    biased;
                    _ = cancellation.cancelled() => return Err(Error::Cancelled),
                    event = stream.next() => event,
                };
                match event {
                    Some(Ok(ProviderEvent::TextDelta(delta))) => {
                        events.emit(EventKind::TextDelta { delta }).await?
                    }
                    Some(Ok(ProviderEvent::Turn(turn))) => break turn,
                    Some(Err(error)) => return Err(error),
                    None => {
                        return Err(Error::Provider(
                            "Provider stream ended without a complete turn".into(),
                        ))
                    }
                }
            };
            drop(stream);
            if cancellation.is_cancelled() {
                return Err(Error::Cancelled);
            }
            if turn.tool_calls.iter().any(|call| {
                !call.arguments.is_object() || call.call_id.is_empty() || call.name.is_empty()
            }) {
                return Err(Error::Provider(
                    "Provider returned an incomplete tool call".into(),
                ));
            }
            history.push(Message::Assistant {
                text: turn.text.clone(),
                tool_calls: turn.tool_calls.clone(),
                replay: turn.replay,
            });
            if turn.tool_calls.is_empty() {
                return Ok(RunResult {
                    text: turn.text,
                    history,
                    output: turn.output,
                });
            }
            for call in turn.tool_calls {
                if cancellation.is_cancelled() {
                    return Err(Error::Cancelled);
                }
                events
                    .emit(EventKind::ToolExecutionStart {
                        tool_call_id: call.call_id.clone(),
                        tool_name: call.name.clone(),
                        arguments: call.arguments.clone(),
                    })
                    .await?;
                let result = match self.tools.get(&call.name) {
                    Some(tool) => tool.execute(call.arguments, cancellation.clone()).await,
                    None => Err(Error::Tool(format!("Unknown tool: {}", call.name))),
                };
                let result = if cancellation.is_cancelled() {
                    Err(Error::Cancelled)
                } else {
                    result
                };
                let is_error = result
                    .as_ref()
                    .map_or(true, |output| output.len() > MAX_TOOL_OUTPUT_BYTES);
                let output = bounded_observation(result.unwrap_or_else(|error| error.to_string()));
                events
                    .emit(EventKind::ToolExecutionEnd {
                        tool_call_id: call.call_id.clone(),
                        tool_name: call.name.clone(),
                        output: output.clone(),
                        is_error,
                    })
                    .await?;
                if cancellation.is_cancelled() {
                    return Err(Error::Cancelled);
                }
                history.push(Message::ToolResult {
                    call_id: call.call_id,
                    name: call.name,
                    output,
                    is_error,
                });
            }
        }
    }
}

struct Events<F> {
    run_id: String,
    sequence: u64,
    failed: bool,
    callback: F,
}

impl<F, Fut> Events<F>
where
    F: FnMut(Event) -> Fut + Send,
    Fut: Future<Output = Result<()>> + Send,
{
    async fn emit(&mut self, kind: EventKind) -> Result<()> {
        self.sequence += 1;
        let result = (self.callback)(Event {
            run_id: self.run_id.clone(),
            sequence: self.sequence,
            kind,
        })
        .await;
        self.failed = result.is_err();
        result
    }
}

fn bounded_observation(output: String) -> String {
    if output.len() > MAX_TOOL_OUTPUT_BYTES {
        "Tool output exceeds the 131072-byte limit".into()
    } else {
        output
    }
}
