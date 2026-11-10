use crate::{
    lines::{LineError, Lines},
    session::Session,
    Agent, CancellationToken, Error, Provider, Result, MAX_JSONL_LINE_BYTES,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::{future::pending, sync::Arc};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    task::JoinHandle,
};

mod output;

use output::{Receipt, Writer};

impl Writer {
    // An oversized object cancels its run while keeping the connection usable.
    fn reply<T: Serialize>(&self, session: &Session, value: T) -> Result<Receipt> {
        let receipt = self.enqueue(value)?;
        if receipt.oversized {
            session.abort();
        }
        Ok(receipt)
    }
}

fn protocol_error(code: &str, message: &str) -> Value {
    json!({"type":"protocol_error","error":{"code":code,"message":message}})
}

fn command_error(id: Option<&str>, command: &str, code: &str, message: &str) -> Value {
    match id {
        Some(id) => {
            json!({"id":id,"type":"response","command":command,"success":false,"error":{"code":code,"message":message}})
        }
        None => protocol_error(code, message),
    }
}

fn response(id: Option<&str>, command: &str, data: Value) -> Value {
    let mut response = json!({"type":"response","command":command,"success":true,"data":data});
    if let Some(id) = id {
        response["id"] = Value::String(id.into());
    }
    response
}

/// Run one native session over bounded UTF-8 JSONL. The caller must consume
/// output concurrently. EOF and cancellation wait for cooperative run cleanup.
pub async fn run_rpc<P, R, W>(
    agent: Arc<Agent<P>>,
    input: R,
    output: W,
    cancellation: CancellationToken,
) -> Result<()>
where
    P: Provider + 'static,
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let cancellation = cancellation.child_token();
    let _cancel_on_drop = cancellation.clone().drop_guard();
    let (writer, output_task) = Writer::start(output, cancellation.clone());
    let mut lines = Lines::new(input);
    let mut session = Arc::new(Session::default());
    let mut task: Option<JoinHandle<()>> = None;
    let result = async {
        loop {
            let line = tokio::select! {
                biased;
                _ = cancellation.cancelled() => break,
                result = async {
                    match &mut task {
                        Some(task) => task.await,
                        None => pending().await,
                    }
                } => {
                    task = None;
                    result.map_err(|error| Error::Consumer(error.to_string()))?;
                    continue;
                }
                line = lines.next() => line,
            };
            let line = match line {
                Ok(Some(line)) => line,
                Ok(None) => break,
                Err(error) => {
                    let response = match error {
                        LineError::TooLarge => protocol_error(
                            "line_too_large",
                            &format!("RPC input exceeds {MAX_JSONL_LINE_BYTES} UTF-8 bytes"),
                        ),
                        LineError::InvalidUtf8 => {
                            protocol_error("invalid_utf8", "RPC input is not valid UTF-8")
                        }
                        LineError::Io(error) => return Err(Error::Consumer(error.to_string())),
                    };
                    writer.reply(&session, response)?;
                    break;
                }
            };
            let command: Value = match serde_json::from_str(&line) {
                Ok(command) => command,
                Err(_) => {
                    writer.reply(
                        &session,
                        protocol_error("invalid_json", "RPC input is not valid JSON"),
                    )?;
                    continue;
                }
            };
            let Some(command) = command.as_object() else {
                writer.reply(
                    &session,
                    protocol_error("invalid_command", "RPC command must be an object"),
                )?;
                continue;
            };
            let id = match command.get("id") {
                Some(Value::String(id)) => Some(id.as_str()),
                None => None,
                _ => {
                    writer.reply(
                        &session,
                        protocol_error("invalid_command", "command.id must be a string"),
                    )?;
                    continue;
                }
            };
            let kind = command
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            match kind {
                "prompt" => {
                    let Some(message) = command
                        .get("message")
                        .and_then(Value::as_str)
                        .filter(|message| !message.is_empty())
                    else {
                        writer.reply(
                            &session,
                            command_error(
                                id,
                                kind,
                                "invalid_command",
                                "prompt.message must be a non-empty string",
                            ),
                        )?;
                        continue;
                    };
                    let run = match session.begin(&cancellation) {
                        Ok(run) => run,
                        Err(_) => {
                            writer.reply(
                                &session,
                                command_error(id, kind, "busy", "A run is already active"),
                            )?;
                            continue;
                        }
                    };
                    if let Some(previous) = task.take() {
                        previous
                            .await
                            .map_err(|error| Error::Consumer(error.to_string()))?;
                    }
                    let acknowledgement =
                        writer.reply(&session, response(id, kind, json!({"runId":run.id})))?;
                    if acknowledgement.oversized {
                        drop(run);
                        continue;
                    }
                    let (agent, writer, message) =
                        (agent.clone(), writer.clone(), message.to_owned());
                    let run_cancellation = run.cancellation.clone();
                    task = Some(tokio::spawn(async move {
                        if writer
                            .wait(acknowledgement, &run_cancellation)
                            .await
                            .is_err()
                        {
                            return;
                        }
                        let _ = run
                            .execute(&agent, &message, move |event| {
                                let writer = writer.clone();
                                let cancellation = run_cancellation.clone();
                                async move {
                                    let receipt = writer.enqueue(event)?;
                                    writer.wait(receipt, &cancellation).await
                                }
                            })
                            .await;
                    }));
                }
                "abort" => {
                    let run_id = session.abort();
                    writer.reply(
                        &session,
                        response(
                            id,
                            kind,
                            json!({"accepted":run_id.is_some(),"runId":run_id}),
                        ),
                    )?;
                }
                "new_session" => {
                    let aborted = session.close().await;
                    if let Some(previous) = task.take() {
                        previous
                            .await
                            .map_err(|error| Error::Consumer(error.to_string()))?;
                    }
                    session = Arc::new(Session::default());
                    writer.reply(&session, response(id, kind, json!({"abortedRun":aborted})))?;
                }
                "get_state" => {
                    writer.reply(
                        &session,
                        response(id, kind, serde_json::to_value(session.state()).unwrap()),
                    )?;
                }
                _ => {
                    writer.reply(
                        &session,
                        command_error(id, kind, "invalid_command", "Unknown command type"),
                    )?;
                }
            }
        }
        Ok(())
    }
    .await;
    session.close().await;
    if let Some(task) = task {
        task.await
            .map_err(|error| Error::Consumer(error.to_string()))?;
    }
    writer.close(output_task).await?;
    result
}

#[cfg(test)]
mod tests;
