mod anthropic;
mod openai;
mod sse;

pub use anthropic::AnthropicProvider;
pub use openai::OpenAIProvider;

use crate::{Error, ProviderEvent, ProviderStream, Result};
use futures_util::{stream, stream::BoxStream, StreamExt};
use reqwest::{Client, Request};
use serde_json::Value;
use std::time::Duration;

type Bytes = BoxStream<'static, Result<Vec<u8>>>;

fn client() -> Result<Client> {
    Client::builder()
        .timeout(Duration::from_secs(180))
        .build()
        .map_err(|error| Error::Configuration(error.to_string()))
}

async fn send(client: &Client, request: Request) -> Result<Bytes> {
    let mut response = client.execute(request).await.map_err(provider_error)?;
    if !response.status().is_success() {
        let status = response.status();
        let mut body = Vec::new();
        while body.len() < 65_536 {
            let Some(chunk) = response.chunk().await.map_err(provider_error)? else {
                break;
            };
            body.extend_from_slice(&chunk[..chunk.len().min(65_536 - body.len())]);
        }
        return Err(Error::Provider(format!(
            "Provider HTTP {status}: {}",
            String::from_utf8_lossy(&body)
        )));
    }
    Ok(response
        .bytes_stream()
        .map(|chunk| chunk.map(|bytes| bytes.to_vec()).map_err(provider_error))
        .boxed())
}

trait Parser: Send + 'static {
    fn event(&mut self, event: Value) -> Result<Option<ProviderEvent>>;
}

fn parse(bytes: Bytes, parser: impl Parser) -> ProviderStream {
    stream::try_unfold(
        (sse::Reader::new(bytes), parser, false),
        |(mut reader, mut parser, finished)| async move {
            if finished {
                return Ok(None);
            }
            while let Some(value) = reader.next().await? {
                if let Some(event) = parser.event(value)? {
                    let finished = matches!(event, ProviderEvent::Turn(_));
                    return Ok(Some((event, (reader, parser, finished))));
                }
            }
            Err(Error::Provider(
                "Provider stream ended before its terminal event".into(),
            ))
        },
    )
    .boxed()
}

fn provider_error(error: impl std::fmt::Display) -> Error {
    Error::Provider(error.to_string())
}

fn string<'a>(value: &'a Value, field: &str) -> Result<&'a str> {
    value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| Error::Provider(format!("Provider field {field} must be a string")))
}

fn index(value: &Value, field: &str) -> Result<usize> {
    value
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|index| usize::try_from(index).ok())
        .ok_or_else(|| {
            Error::Provider(format!(
                "Provider field {field} must be a nonnegative index"
            ))
        })
}

#[cfg(test)]
mod tests;
