use futures_util::{stream, StreamExt};
use serde_json::{json, Value};
use std::{sync::Arc, time::Duration};
use tiny_agent::{
    serve, Agent, AgentConfig, HttpOptions, Message, Provider, ProviderEvent, ProviderRequest,
    ProviderStream, ProviderTurn, Result,
};
use tokio::time::timeout;

struct Fixture;

impl Provider for Fixture {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        match request.history.last() {
            Some(Message::User { text }) if text == "wait" => Ok(Box::pin(
                stream::iter([Ok(ProviderEvent::TextDelta("waiting".into()))])
                    .chain(stream::pending()),
            )),
            Some(Message::User { text }) if text == "flood" => {
                Ok(Box::pin(stream::repeat_with(|| {
                    Ok(ProviderEvent::TextDelta("x".repeat(64 * 1024)))
                })))
            }
            _ => Ok(Box::pin(stream::iter([
                Ok(ProviderEvent::TextDelta("done".into())),
                Ok(ProviderEvent::Turn(ProviderTurn {
                    text: format!("history={}", request.history.len()),
                    tool_calls: vec![],
                    replay: vec![],
                    output: vec![],
                })),
            ]))),
        }
    }
}

async fn server() -> tiny_agent::HttpServer {
    let agent = Agent::new(AgentConfig {
        model: "fixture".into(),
        instructions: String::new(),
        provider: Fixture,
        tools: vec![],
    })
    .unwrap();
    serve(
        Arc::new(agent),
        HttpOptions {
            address: "127.0.0.1:0".parse().unwrap(),
            token: "secret".into(),
            allow_remote: false,
        },
    )
    .await
    .expect("real TCP conformance requires local binding")
}

async fn create(client: &reqwest::Client, url: &str) -> String {
    let response: Value = client
        .post(format!("{url}/sessions"))
        .bearer_auth("secret")
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    response["sessionId"].as_str().unwrap().to_owned()
}

async fn prompt(client: &reqwest::Client, url: &str, id: &str, message: &str) -> reqwest::Response {
    client
        .post(format!("{url}/sessions/{id}/prompts"))
        .bearer_auth("secret")
        .json(&json!({"message":message}))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
}

async fn events(response: reqwest::Response) -> Vec<Value> {
    response
        .text()
        .await
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

async fn wait_for_delta(response: &mut reqwest::Response) {
    let mut bytes = Vec::new();
    timeout(Duration::from_secs(2), async {
        while !String::from_utf8_lossy(&bytes).contains("waiting") {
            bytes.extend_from_slice(&response.chunk().await.unwrap().expect("missing delta"));
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn local_http_routes_cancel_disconnect_and_shutdown() {
    let server = server().await;
    let url = server.url();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap();
    assert_eq!(
        client
            .get(format!("{url}/health"))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let health = client
        .get(format!("{url}/health"))
        .bearer_auth("secret")
        .send()
        .await
        .unwrap();
    assert!(!health.headers().contains_key("access-control-allow-origin"));
    assert_eq!(
        health.json::<Value>().await.unwrap(),
        json!({"status":"ok","protocolVersion":1})
    );
    let id = create(&client, &url).await;
    let first = events(prompt(&client, &url, &id, "finish").await).await;
    assert_eq!(first[0]["type"], "agent_start");
    assert_eq!(first.last().unwrap()["text"], "history=1");
    let mut waiting = prompt(&client, &url, &id, "wait").await;
    wait_for_delta(&mut waiting).await;
    let busy = client
        .post(format!("{url}/sessions/{id}/prompts"))
        .bearer_auth("secret")
        .json(&json!({"message":"busy"}))
        .send()
        .await
        .unwrap();
    assert_eq!(busy.status(), 409);
    let cancel: Value = client
        .post(format!("{url}/sessions/{id}/cancel"))
        .bearer_auth("secret")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(cancel["accepted"], true);
    assert_eq!(events(waiting).await.last().unwrap()["status"], "aborted");
    let second = events(prompt(&client, &url, &id, "finish").await).await;
    assert_eq!(second.last().unwrap()["text"], "history=3");
    let mut disconnected = prompt(&client, &url, &id, "wait").await;
    wait_for_delta(&mut disconnected).await;
    drop(disconnected);
    // Deletion must wait for the disconnected provider and release the session.
    assert_eq!(
        client
            .delete(format!("{url}/sessions/{id}"))
            .bearer_auth("secret")
            .send()
            .await
            .unwrap()
            .status(),
        204
    );
    let id = create(&client, &url).await;
    let mut active = prompt(&client, &url, &id, "wait").await;
    wait_for_delta(&mut active).await;
    timeout(Duration::from_secs(2), server.close())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(events(active).await.last().unwrap()["status"], "aborted");
    server.close().await.unwrap();
    assert!(client.get(format!("{url}/health")).send().await.is_err());
}

#[tokio::test]
async fn local_http_backpressure_does_not_hold_shutdown() {
    let server = server().await;
    let url = server.url();
    let client = reqwest::Client::new();
    let id = create(&client, &url).await;
    let response = prompt(&client, &url, &id, "flood").await;
    tokio::time::sleep(Duration::from_millis(50)).await;
    timeout(Duration::from_secs(2), server.close())
        .await
        .expect("blocked peer held shutdown")
        .unwrap();
    drop(response);
}

#[cfg(unix)]
#[tokio::test]
async fn local_http_cli_serves_and_exits_on_interrupt() {
    use std::process::Stdio;
    use tokio::io::{AsyncBufReadExt, BufReader};
    let mut child = tokio::process::Command::new(env!("CARGO_BIN_EXE_tiny-agent"))
        .args(["serve", "--port", "0", "--token", "secret"])
        .env_remove("OPENAI_API_KEY")
        .env_remove("ANTHROPIC_API_KEY")
        .env("TINYAGENT_PROVIDER", "openai")
        .env_remove("TINYAGENT_MODEL")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let mut diagnostics = BufReader::new(child.stderr.take().unwrap()).lines();
    let line = timeout(Duration::from_secs(3), diagnostics.next_line())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let url = line
        .strip_prefix("TinyAgent listening on ")
        .unwrap_or_else(|| panic!("real CLI HTTP conformance requires local binding: {line}"));
    let response = reqwest::Client::new()
        .get(format!("{url}/health"))
        .bearer_auth("secret")
        .send()
        .await
        .unwrap();
    assert_eq!(response.json::<Value>().await.unwrap()["status"], "ok");
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(child.id().unwrap() as i32),
        nix::sys::signal::Signal::SIGINT,
    )
    .unwrap();
    assert!(timeout(Duration::from_secs(2), child.wait())
        .await
        .unwrap()
        .unwrap()
        .success());
    assert!(diagnostics.next_line().await.unwrap().is_none());
}
