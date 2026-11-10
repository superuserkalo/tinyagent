use super::*;
use crate::{Message, ProviderEvent, ProviderTurn, ToolCall};
use futures_util::stream;
use serde_json::json;
use std::sync::Mutex as StdMutex;

struct FixtureProvider {
    requests: Arc<StdMutex<Vec<Vec<Message>>>>,
}

impl Provider for FixtureProvider {
    async fn stream(&self, request: ProviderRequest) -> Result<ProviderStream> {
        self.requests.lock().unwrap().push(request.history.clone());
        let last = request.history.last().unwrap();
        if matches!(last, Message::User { text } if text == "fail") {
            return Ok(Box::pin(stream::iter([
                Ok(ProviderEvent::TextDelta("partial".into())),
                Err(Error::Provider("fixture failure".into())),
            ])));
        }
        let tool = matches!(last, Message::User { text } if text == "tool");
        let from_tool = matches!(last, Message::ToolResult { .. });
        let text = if from_tool { "tool result" } else { "hello" };
        let turn = ProviderTurn {
            text: if tool { String::new() } else { text.into() },
            tool_calls: if tool {
                vec![ToolCall {
                    call_id: "bash-1".into(),
                    name: "bash".into(),
                    arguments: json!({"command":"printf observation"}),
                }]
            } else {
                vec![]
            },
            replay: vec![],
            output: vec![],
        };
        let mut events = Vec::new();
        if !from_tool {
            events.push(Ok(ProviderEvent::TextDelta(text.into())));
        }
        events.push(Ok(ProviderEvent::Turn(turn)));
        Ok(Box::pin(stream::iter(events)))
    }
}

#[tokio::test]
async fn interactive_streams_once_runs_native_tools_and_rolls_back_failure() {
    let requests = Arc::new(StdMutex::new(Vec::new()));
    let agent = Agent::new(AgentConfig {
        model: "fixture".into(),
        instructions: String::new(),
        provider: FixtureProvider {
            requests: requests.clone(),
        },
        tools: vec![bash_tool().unwrap()],
    })
    .unwrap();
    let (mut output, mut diagnostics) = (Vec::new(), Vec::new());
    interactive(
        &agent,
        &b"first\nfail\ntool\n"[..],
        &mut output,
        &mut diagnostics,
        CancellationToken::new(),
    )
    .await
    .unwrap();
    let output = String::from_utf8(output).unwrap();
    assert_eq!(output.matches("hello").count(), 2, "{output}");
    assert_eq!(output.matches("tool result").count(), 1, "{output}");
    assert!(output.contains("partial"));
    let diagnostics = String::from_utf8(diagnostics).unwrap();
    assert!(diagnostics.contains("error: fixture failure"));
    assert!(diagnostics.contains("$ printf observation"));
    assert!(diagnostics.contains("stdout:\nobservation"));
    let requests = requests.lock().unwrap();
    assert_eq!(requests.len(), 4);
    assert_eq!(requests[1].len(), 3);
    assert_eq!(requests[2].len(), 3, "failed prompt must not persist");
    assert!(!requests[2]
        .iter()
        .any(|message| matches!(message, Message::User { text } if text == "fail")));
    assert_eq!(
        requests[3].len(),
        5,
        "native tool result must return to provider"
    );
}

#[tokio::test]
async fn cli_validates_options_and_self_test_without_provider_credentials() {
    for args in [
        vec!["--mode"],
        vec!["--mode", "invalid"],
        vec!["unexpected"],
    ] {
        let args = args.into_iter().map(str::to_owned).collect::<Vec<_>>();
        assert!(matches!(
            run(
                &args,
                tokio::io::empty(),
                tokio::io::sink(),
                tokio::io::sink(),
                CancellationToken::new()
            )
            .await,
            Err(Error::Configuration(_))
        ));
    }
    run(
        &["--self-test".into()],
        tokio::io::empty(),
        tokio::io::sink(),
        tokio::io::sink(),
        CancellationToken::new(),
    )
    .await
    .unwrap();
}

#[test]
fn serve_options_validate_host_port_and_unknown_flags() {
    let parse = |args: &[&str]| {
        parse_http_options(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>())
    };
    let options = parse(&[
        "--host",
        "::1",
        "--port",
        "0",
        "--token",
        "secret",
        "--allow-remote",
    ])
    .unwrap();
    assert_eq!(options.address, "[::1]:0".parse().unwrap());
    assert_eq!(options.token, "secret");
    assert!(options.allow_remote);
    assert_eq!(
        parse(&["--host", "localhost"]).unwrap().address.ip(),
        "127.0.0.1".parse::<IpAddr>().unwrap()
    );
    for args in [
        &["--port"][..],
        &["--port", "-1"],
        &["--port", "65536"],
        &["--host", "example.com"],
        &["--unknown"],
        &["extra"],
    ] {
        assert!(parse(args).is_err(), "{args:?}");
    }
}
