use crate::{CancellationToken, Error, Result, ToolDefinition};
use futures_util::future::BoxFuture;
use jsonschema::{Draft, Retrieve, Uri, Validator};
use serde_json::Value;
use std::future::Future;

type Handler = dyn Fn(Value, CancellationToken) -> BoxFuture<'static, Result<String>> + Send + Sync;

pub struct Tool {
    definition: ToolDefinition,
    validator: Validator,
    handler: Box<Handler>,
}

impl Tool {
    pub fn new<F, Fut>(definition: ToolDefinition, handler: F) -> Result<Self>
    where
        F: Fn(Value, CancellationToken) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String>> + Send + 'static,
    {
        if definition.name.is_empty() || definition.parameters.get("$async").is_some() {
            return Err(Error::Configuration(
                "Tool name is required and async schemas are unsupported".into(),
            ));
        }
        let validator = jsonschema::options()
            .with_draft(Draft::Draft202012)
            .should_validate_formats(false)
            .with_retriever(NoExternalSchemas)
            .build(&definition.parameters)
            .map_err(|error| Error::Configuration(error.to_string()))?;
        Ok(Self {
            definition,
            validator,
            handler: Box::new(move |arguments, cancellation| {
                Box::pin(handler(arguments, cancellation))
            }),
        })
    }

    pub fn definition(&self) -> ToolDefinition {
        self.definition.clone()
    }

    pub async fn execute(
        &self,
        arguments: Value,
        cancellation: CancellationToken,
    ) -> Result<String> {
        if cancellation.is_cancelled() {
            return Err(Error::Cancelled);
        }
        if !arguments.is_object() {
            return Err(Error::Tool("Tool arguments must be a JSON object".into()));
        }
        self.validator
            .validate(&arguments)
            .map_err(|error| Error::Tool(format!("Invalid tool arguments: {error}")))?;
        (self.handler)(arguments, cancellation).await
    }
}

struct NoExternalSchemas;

impl Retrieve for NoExternalSchemas {
    fn retrieve(
        &self,
        uri: &Uri<String>,
    ) -> std::result::Result<Value, Box<dyn std::error::Error + Send + Sync>> {
        Err(format!("External schemas are disabled: {uri}").into())
    }
}
