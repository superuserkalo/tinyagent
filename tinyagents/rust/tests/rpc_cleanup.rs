use serde_json::json;
use std::{future::pending, sync::Arc, time::Duration};
use tiny_agent::{
    run_rpc, Agent, AgentConfig, CancellationToken, Error, Provider, ProviderRequest,
    ProviderStream, Result,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::Notify,
    time::timeout,
};

struct WaitingProvider {
    started: Arc<Notify>,
    stopped: Arc<Notify>,
}

struct NotifyOnDrop(Arc<Notify>);

impl Drop for NotifyOnDrop {
    fn drop(&mut self) {
        self.0.notify_one();
    }
}

impl Provider for WaitingProvider {
    async fn stream(&self, _: ProviderRequest) -> Result<ProviderStream> {
        let _guard = NotifyOnDrop(self.stopped.clone());
        self.started.notify_one();
        pending().await
    }
}

fn agent(provider: WaitingProvider) -> Arc<Agent<WaitingProvider>> {
    Arc::new(
        Agent::new(AgentConfig {
            model: "fixture".into(),
            instructions: String::new(),
            provider,
            tools: vec![],
        })
        .unwrap(),
    )
}

#[tokio::test]
async fn dropping_rpc_future_cancels_the_active_provider() {
    let (started, stopped) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
    let agent = agent(WaitingProvider {
        started: started.clone(),
        stopped: stopped.clone(),
    });
    let (mut input, reader) = tokio::io::duplex(1024);
    let task = tokio::spawn(run_rpc(
        agent,
        reader,
        tokio::io::sink(),
        CancellationToken::new(),
    ));
    input
        .write_all(b"{\"type\":\"prompt\",\"message\":\"wait\"}\n")
        .await
        .unwrap();
    timeout(Duration::from_secs(1), started.notified())
        .await
        .unwrap();
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    timeout(Duration::from_secs(1), stopped.notified())
        .await
        .expect("dropping driver left a detached provider running");
}

#[tokio::test]
async fn cancellation_bounds_a_blocked_writer_and_disconnect_returns_an_error() {
    for disconnected in [false, true] {
        let agent = agent(WaitingProvider {
            started: Arc::new(Notify::new()),
            stopped: Arc::new(Notify::new()),
        });
        let (output, reader) = tokio::io::duplex(1);
        let reader = if disconnected {
            drop(reader);
            None
        } else {
            Some(reader)
        };
        let cancellation = CancellationToken::new();
        let token = cancellation.clone();
        let cancel = async move {
            tokio::time::sleep(Duration::from_millis(10)).await;
            token.cancel();
        };
        let run = run_rpc(
            agent,
            &b"{\"type\":\"get_state\"}\n"[..],
            output,
            cancellation,
        );
        let (result, ()) = timeout(Duration::from_millis(1500), async {
            tokio::join!(run, cancel)
        })
        .await
        .expect("RPC writer did not settle");
        assert!(matches!(result, Err(Error::Consumer(_))), "{result:?}");
        drop(reader);
    }
}

#[tokio::test]
async fn eof_behind_a_blocked_reply_or_acknowledgement_settles() {
    for kind in ["get_state", "prompt"] {
        let started = Arc::new(Notify::new());
        let agent = agent(WaitingProvider {
            started: started.clone(),
            stopped: Arc::new(Notify::new()),
        });
        let input = format!("{}\n", json!({"type":kind,"message":"wait"}));
        let (output, _reader) = tokio::io::duplex(1);
        let result = timeout(
            Duration::from_millis(1500),
            run_rpc(agent, input.as_bytes(), output, CancellationToken::new()),
        )
        .await
        .expect("EOF was hidden behind a blocked RPC write");
        assert!(matches!(result, Err(Error::Consumer(_))), "{result:?}");
        assert!(
            timeout(Duration::from_millis(10), started.notified())
                .await
                .is_err(),
            "provider started before its acknowledgement was delivered"
        );
    }
}

#[tokio::test]
async fn abort_behind_a_blocked_acknowledgement_settles_without_eof() {
    let started = Arc::new(Notify::new());
    let agent = agent(WaitingProvider {
        started: started.clone(),
        stopped: Arc::new(Notify::new()),
    });
    let (mut input, reader) = tokio::io::duplex(1024);
    input
        .write_all(b"{\"type\":\"prompt\",\"message\":\"wait\"}\n{\"type\":\"abort\"}\n")
        .await
        .unwrap();
    let (output, _reader) = tokio::io::duplex(1);
    let result = timeout(
        Duration::from_millis(1500),
        run_rpc(agent, reader, output, CancellationToken::new()),
    )
    .await
    .expect("abort was hidden behind a blocked acknowledgement");
    assert!(matches!(result, Err(Error::Consumer(_))), "{result:?}");
    assert!(timeout(Duration::from_millis(10), started.notified())
        .await
        .is_err());
    drop(input);
}

#[tokio::test]
async fn abort_behind_a_blocked_state_reply_stops_the_provider() {
    let (started, stopped) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
    let agent = agent(WaitingProvider {
        started: started.clone(),
        stopped: stopped.clone(),
    });
    let (mut input, reader) = tokio::io::duplex(4096);
    let (output, receiver) = tokio::io::duplex(256);
    let task = tokio::spawn(run_rpc(agent, reader, output, CancellationToken::new()));
    input
        .write_all(b"{\"type\":\"prompt\",\"message\":\"wait\"}\n")
        .await
        .unwrap();
    let mut receiver = BufReader::new(receiver).lines();
    for _ in 0..2 {
        timeout(Duration::from_secs(1), receiver.next_line())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }
    timeout(Duration::from_secs(1), started.notified())
        .await
        .unwrap();
    let command = format!(
        "{}\n{{\"type\":\"abort\"}}\n",
        json!({"type":"get_state","id":"x".repeat(2048)})
    );
    input.write_all(command.as_bytes()).await.unwrap();
    let cleanup = timeout(Duration::from_millis(500), stopped.notified()).await;
    if cleanup.is_err() {
        task.abort();
        let _ = task.await;
        panic!("abort could not reach the provider behind a state reply");
    }
    let result = timeout(Duration::from_millis(1500), task)
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(result, Err(Error::Consumer(_))), "{result:?}");
}
