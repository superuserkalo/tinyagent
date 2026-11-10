use super::{client, index, parse, provider_error, send, string, Parser};
use crate::{
    Error, Message, Provider, ProviderEvent, ProviderRequest, ProviderStream, ProviderTurn, Result,
    ToolCall, MAX_JSONL_LINE_BYTES,
};
use reqwest::{Client, Request};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};

pub struct OpenAIProvider {
    pub client: Client,
    pub endpoint: String,
    pub api_key: Option<String>,
}

impl OpenAIProvider {
    pub fn new() -> Result<Self> {
        Ok(Self {
            client: client()?,
            endpoint: "https://api.openai.com/v1/responses".into(),
            api_key: None,
        })
    }

    pub(super) fn request(&self, request: ProviderRequest) -> Result<Request> {
        let key = self
            .api_key
            .clone()
            .or_else(|| std::env::var("OPENAI_API_KEY").ok())
            .filter(|key| !key.is_empty())
            .ok_or_else(|| Error::Configuration("OPENAI_API_KEY is not set".into()))?;
        let mut input = Vec::new();
        for message in request.history {
            match message {
                Message::User { text } => input.push(json!({"role":"user", "content":text})),
                Message::Assistant {
                    text,
                    tool_calls,
                    replay,
                } => {
                    if !replay.is_empty() {
                        input.extend(replay);
                        continue;
                    }
                    if !text.is_empty() {
                        input.push(json!({"role":"assistant", "content":text}));
                    }
                    for call in tool_calls {
                        input.push(json!({"type":"function_call", "call_id":call.call_id, "name":call.name, "arguments":call.arguments.to_string()}));
                    }
                }
                Message::ToolResult {
                    call_id, output, ..
                } => input.push(
                    json!({"type":"function_call_output", "call_id":call_id, "output":output}),
                ),
            }
        }
        let tools: Vec<_> = request
            .tools
            .into_iter()
            .map(|tool| {
                json!({"type":"function", "name":tool.name,
            "description":tool.description, "parameters":tool.parameters, "strict":true})
            })
            .collect();
        self.client.post(&self.endpoint).bearer_auth(key).header("Accept", "text/event-stream").json(&json!({
            "model":request.model, "instructions":request.instructions, "input":input, "tools":tools,
            "parallel_tool_calls":false, "store":false, "stream":true,
            "include":["reasoning.encrypted_content"],
        })).build().map_err(provider_error)
    }
}

impl Provider for OpenAIProvider {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        Ok(parse(
            send(&self.client, self.request(request)?).await?,
            Response::default(),
        ))
    }
}

#[derive(Default)]
pub(super) struct Response {
    items: BTreeMap<usize, Value>,
    arguments: BTreeMap<usize, String>,
}

impl Parser for Response {
    fn event(&mut self, event: Value) -> Result<Option<ProviderEvent>> {
        match string(&event, "type")? {
            "response.output_item.added" | "response.output_item.done" => {
                let index = index(&event, "output_index")?;
                let item = event
                    .get("item")
                    .filter(|item| item.is_object())
                    .ok_or_else(|| Error::Provider("Missing OpenAI output item".into()))?;
                if string(item, "type")? == "function_call" {
                    let arguments = string(item, "arguments")?;
                    if !arguments.is_empty() {
                        self.arguments.insert(index, arguments.into());
                    }
                }
                self.items.insert(index, item.clone());
            }
            "response.output_text.delta" => {
                return Ok(Some(ProviderEvent::TextDelta(
                    string(&event, "delta")?.into(),
                )))
            }
            "response.function_call_arguments.delta" => {
                let index = index(&event, "output_index")?;
                let delta = string(&event, "delta")?;
                let arguments = self.arguments.entry(index).or_default();
                if arguments.len() + delta.len() > MAX_JSONL_LINE_BYTES {
                    return Err(Error::Provider(
                        "OpenAI tool arguments exceed the byte limit".into(),
                    ));
                }
                arguments.push_str(delta);
            }
            "response.function_call_arguments.done" => {
                self.arguments.insert(
                    index(&event, "output_index")?,
                    string(&event, "arguments")?.into(),
                );
            }
            "response.completed" => {
                return self
                    .complete(&event)
                    .map(|turn| Some(ProviderEvent::Turn(turn)))
            }
            "response.failed" | "response.incomplete" | "error" => {
                return Err(Error::Provider(format!("OpenAI stream failed: {event}")))
            }
            _ => {}
        }
        Ok(None)
    }
}

impl Response {
    fn complete(&self, event: &Value) -> Result<ProviderTurn> {
        let response = event
            .get("response")
            .filter(|value| value.is_object())
            .ok_or_else(|| Error::Provider("Missing completed OpenAI response".into()))?;
        let items: Vec<(usize, Value)> = match response.get("output") {
            Some(Value::Array(items)) => items.iter().cloned().enumerate().collect(),
            None => self
                .items
                .iter()
                .map(|(index, item)| (*index, item.clone()))
                .collect(),
            _ => return Err(Error::Provider("OpenAI output must be an array".into())),
        };
        let mut output = Vec::new();
        let mut calls = Vec::new();
        let mut text = String::new();
        let mut ids = BTreeSet::new();
        for (index, mut item) in items {
            match string(&item, "type")? {
                "function_call" => {
                    let raw = match item.get("arguments") {
                        Some(Value::String(raw)) if !raw.is_empty() => raw.clone(),
                        None | Some(Value::String(_)) => {
                            self.arguments.get(&index).cloned().unwrap_or_default()
                        }
                        _ => {
                            return Err(Error::Provider(
                                "OpenAI arguments must be a JSON string".into(),
                            ))
                        }
                    };
                    let arguments: Value = serde_json::from_str(&raw).map_err(provider_error)?;
                    let call_id = string(&item, "call_id")?.to_owned();
                    let name = string(&item, "name")?.to_owned();
                    if raw.len() > MAX_JSONL_LINE_BYTES
                        || !arguments.is_object()
                        || call_id.is_empty()
                        || name.is_empty()
                        || !ids.insert(call_id.clone())
                    {
                        return Err(Error::Provider("Invalid OpenAI function call".into()));
                    }
                    item["arguments"] = Value::String(raw);
                    calls.push(ToolCall {
                        call_id,
                        name,
                        arguments,
                    });
                }
                "message" => {
                    let content =
                        item.get("content")
                            .and_then(Value::as_array)
                            .ok_or_else(|| {
                                Error::Provider("OpenAI message is missing content".into())
                            })?;
                    for part in content {
                        if string(part, "type")? == "output_text" {
                            text.push_str(string(part, "text")?);
                        }
                    }
                }
                _ => {}
            }
            output.push(item);
        }
        Ok(ProviderTurn {
            text,
            tool_calls: calls,
            replay: output.clone(),
            output,
        })
    }
}
