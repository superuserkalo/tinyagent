use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, OnceLock},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tiny_agent::MAX_JSONL_LINE_BYTES;

fn fixture_binary() -> &'static Path {
    static BINARY: OnceLock<PathBuf> = OnceLock::new();
    BINARY.get_or_init(|| {
        let output = Command::new(env!("CARGO"))
            .args([
                "build",
                "--offline",
                "--locked",
                "--example",
                "rpc_fixture",
                "--message-format=json",
            ])
            .current_dir(env!("CARGO_MANIFEST_DIR"))
            .env("CARGO_INCREMENTAL", "0")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "fixture build failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .filter_map(|line| {
                let artifact: Value = serde_json::from_str(line).ok()?;
                artifact["executable"].as_str().map(PathBuf::from)
            })
            .next_back()
            .expect("fixture executable")
    })
}

struct Process {
    child: Child,
    input: Option<ChildStdin>,
    messages: mpsc::Receiver<Value>,
    reader: Option<JoinHandle<()>>,
    diagnostics: Option<JoinHandle<String>>,
    finished: bool,
}

impl Process {
    fn start(binary: &Path, args: &[&str]) -> Self {
        let mut child = Command::new(binary)
            .args(args)
            .env_remove("OPENAI_API_KEY")
            .env_remove("ANTHROPIC_API_KEY")
            .env("TINYAGENT_PROVIDER", "openai")
            .env("TINYAGENT_MODEL", "fixture")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let input = child.stdin.take();
        let stdout = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let (sender, messages) = mpsc::channel();
        let reader = thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let line = line.unwrap();
                assert!(line.len() <= MAX_JSONL_LINE_BYTES);
                let value = serde_json::from_str(&line).expect("stdout must contain only JSONL");
                if sender.send(value).is_err() {
                    break;
                }
            }
        });
        let diagnostics = thread::spawn(move || {
            let mut text = String::new();
            stderr.read_to_string(&mut text).unwrap();
            text
        });
        Self {
            child,
            input,
            messages,
            reader: Some(reader),
            diagnostics: Some(diagnostics),
            finished: false,
        }
    }

    fn fixture() -> Self {
        Self::start(fixture_binary(), &[])
    }

    fn raw(&mut self, bytes: &[u8]) {
        self.input.as_mut().unwrap().write_all(bytes).unwrap();
        self.input.as_mut().unwrap().flush().unwrap();
    }

    fn send(&mut self, value: Value) {
        let mut line = serde_json::to_vec(&value).unwrap();
        line.push(b'\n');
        self.raw(&line);
    }

    fn next(&self) -> Value {
        self.messages
            .recv_timeout(Duration::from_secs(3))
            .expect("RPC response timed out or output ended")
    }

    fn until(&self, kind: &str) -> Vec<Value> {
        let mut messages = Vec::new();
        loop {
            let message = self.next();
            let done = message["type"] == kind;
            messages.push(message);
            if done {
                return messages;
            }
        }
    }

    fn idle(&mut self, message_count: usize) {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            self.send(json!({"id":"state","type":"get_state"}));
            let state = self.next();
            assert_eq!(state["id"], "state", "{state}");
            if state["data"]["status"] == "idle" {
                assert_eq!(state["data"]["messageCount"], message_count);
                return;
            }
            assert!(Instant::now() < deadline, "session stayed busy");
            thread::sleep(Duration::from_millis(1));
        }
    }

    fn finish(&mut self) {
        self.input.take();
        let deadline = Instant::now() + Duration::from_secs(3);
        let status = loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                break status;
            }
            assert!(Instant::now() < deadline, "RPC process did not exit");
            thread::sleep(Duration::from_millis(5));
        };
        self.finished = true;
        self.reader.take().unwrap().join().unwrap();
        let stderr = self.diagnostics.take().unwrap().join().unwrap();
        assert!(status.success(), "{status}: {stderr}");
        assert!(stderr.is_empty(), "unexpected stderr: {stderr}");
        assert!(
            self.messages.try_recv().is_err(),
            "unexpected trailing output"
        );
    }
}

impl Drop for Process {
    fn drop(&mut self) {
        if !self.finished {
            self.input.take();
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

#[test]
fn shared_static_commands_and_recoverable_json_errors() {
    let fixture: Value = serde_json::from_str(include_str!("../conformance/rpc_v1.json")).unwrap();
    let mut process = Process::fixture();
    for scenario in fixture["scenarios"].as_array().unwrap().iter().take(6) {
        for command in scenario["commands"].as_array().unwrap() {
            process.send(command.clone());
        }
        for expected in scenario["expected"].as_array().unwrap() {
            assert_eq!(process.next(), *expected, "{}", scenario["name"]);
        }
    }
    process.raw(b"{\n");
    assert_eq!(process.next()["error"]["code"], "invalid_json");
    process.send(json!([]));
    assert_eq!(process.next()["error"]["code"], "invalid_command");
    process.idle(0);
    process.finish();
}

#[test]
fn completion_order_identity_newline_framing_and_failed_history_rollback() {
    let mut process = Process::fixture();
    process.send(json!({"id":"prompt","type":"prompt","message":"finish"}));
    let ack = process.next();
    assert_eq!(ack["type"], "response");
    assert_eq!(ack["id"], "prompt");
    let events = process.until("agent_end");
    assert_eq!(
        events
            .iter()
            .map(|event| event["type"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["agent_start", "text_delta", "agent_end"]
    );
    for (index, event) in events.iter().enumerate() {
        assert_eq!(event["runId"], ack["data"]["runId"]);
        assert_eq!(event["sequence"], index + 1);
    }
    assert_eq!(events[1]["delta"], "done\nfixture");
    assert_eq!(events[2]["status"], "completed");
    process.idle(2);
    process.send(json!({"type":"prompt","message":"fail"}));
    assert_eq!(
        process.until("agent_end").last().unwrap()["status"],
        "failed"
    );
    process.idle(2);
    process.finish();
}

#[test]
fn busy_abort_and_reset_wait_for_cleanup() {
    let mut process = Process::fixture();
    process.send(json!({"type":"prompt","message":"wait"}));
    process.until("text_delta");
    process.send(json!({"id":"busy","type":"prompt","message":"second"}));
    assert_eq!(process.next()["error"]["code"], "busy");
    process.send(json!({"id":"abort","type":"abort"}));
    let (mut acknowledged, mut ended) = (false, false);
    while !acknowledged || !ended {
        let message = process.next();
        if message["id"] == "abort" {
            assert_eq!(message["data"]["accepted"], true);
            acknowledged = true;
        }
        if message["type"] == "agent_end" {
            assert_eq!(message["status"], "aborted");
            ended = true;
        }
    }
    process.idle(0);
    process.send(json!({"type":"prompt","message":"wait"}));
    process.until("text_delta");
    process.send(json!({"id":"reset","type":"new_session"}));
    assert_eq!(
        process.until("agent_end").last().unwrap()["status"],
        "aborted"
    );
    let response = process.next();
    assert_eq!(response["id"], "reset");
    assert_eq!(response["data"]["abortedRun"], true);
    process.idle(0);
    process.finish();
}

#[test]
fn oversized_events_and_responses_leave_rpc_usable() {
    let mut process = Process::fixture();
    process.send(json!({"type":"prompt","message":"oversized"}));
    assert_eq!(
        process.until("protocol_error").last().unwrap()["error"]["code"],
        "output_too_large"
    );
    process.idle(0);
    for kind in ["get_state", "prompt"] {
        let command =
            json!({"id":"x".repeat(MAX_JSONL_LINE_BYTES - 100), "type":kind, "message":"finish"});
        assert!(serde_json::to_vec(&command).unwrap().len() < MAX_JSONL_LINE_BYTES);
        process.send(command);
        assert_eq!(process.next()["error"]["code"], "output_too_large");
        process.idle(0);
    }
    process.send(json!({"type":"prompt","message":"wait"}));
    process.until("text_delta");
    process.send(json!({"id":"x".repeat(MAX_JSONL_LINE_BYTES - 100),"type":"get_state"}));
    assert_eq!(process.next()["error"]["code"], "output_too_large");
    assert_eq!(
        process.until("agent_end").last().unwrap()["status"],
        "aborted"
    );
    process.idle(0);
    process.send(json!({"type":"prompt","message":"finish"}));
    assert_eq!(
        process.until("agent_end").last().unwrap()["status"],
        "completed"
    );
    process.idle(2);
    process.finish();
}

#[test]
fn partial_command_survives_run_completion() {
    let mut process = Process::fixture();
    process.send(json!({"type":"prompt","message":"delayed"}));
    process.until("text_delta");
    process.raw(b"{\"id\":\"state\",");
    assert_eq!(
        process.until("agent_end").last().unwrap()["status"],
        "completed"
    );
    process.raw(b"\"type\":\"get_state\"}\n");
    let state = process.next();
    assert_eq!(state["id"], "state");
    // Reading agent_end can precede completion of the writer's flush. The
    // session remains running until that delivery receipt permits the commit.
    match state["data"]["status"].as_str() {
        Some("running") => assert_eq!(state["data"]["messageCount"], 0),
        Some("idle") => assert_eq!(state["data"]["messageCount"], 2),
        status => panic!("unexpected session status: {status:?}"),
    }
    process.idle(2);
    process.finish();
}

fn cancel_bash(interrupt: bool) {
    let mut process = Process::fixture();
    process.send(json!({"type":"prompt","message":"wait_tool"}));
    let started = process.until("tool_execution_start").pop().unwrap();
    // Give the shell time to exit and leave its descendant holding the pipes.
    thread::sleep(Duration::from_millis(100));
    if interrupt {
        #[cfg(unix)]
        nix::sys::signal::kill(
            nix::unistd::Pid::from_raw(process.child.id() as i32),
            nix::sys::signal::Signal::SIGINT,
        )
        .unwrap();
    } else {
        process.input.take();
    }
    let events = process.until("agent_end");
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["type"], "tool_execution_end");
    assert_eq!(events[0]["toolCallId"], started["toolCallId"]);
    assert_eq!(events[0]["isError"], true);
    assert_eq!(events[1]["status"], "aborted");
    process.finish();
}

#[test]
fn eof_cancels_bash_and_pairs_tool_events() {
    cancel_bash(false);
}

#[cfg(unix)]
#[test]
fn interrupt_cancels_bash_and_pairs_tool_events() {
    cancel_bash(true);
}

#[test]
fn real_cli_state_without_credentials_and_fatal_framing() {
    for (input, expected) in [
        (
            b"{\"id\":\"state\",\"type\":\"get_state\"}\r\n".to_vec(),
            "state",
        ),
        (vec![0xff, b'\n'], "invalid_utf8"),
        (vec![b' '; MAX_JSONL_LINE_BYTES + 1], "line_too_large"),
    ] {
        let mut process = Process::start(
            Path::new(env!("CARGO_BIN_EXE_tiny-agent")),
            &["--mode", "rpc"],
        );
        process.raw(&input);
        let response = process.next();
        if expected == "state" {
            assert_eq!(response["data"]["status"], "idle");
        } else {
            assert_eq!(response["error"]["code"], expected);
        }
        process.finish();
    }
}

#[cfg(unix)]
#[test]
fn real_cli_interrupt_exits_with_stdin_still_open() {
    let mut process = Process::start(
        Path::new(env!("CARGO_BIN_EXE_tiny-agent")),
        &["--mode", "rpc"],
    );
    process.send(json!({"type":"get_state"}));
    process.next();
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(process.child.id() as i32),
        nix::sys::signal::Signal::SIGINT,
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while process.child.try_wait().unwrap().is_none() {
        assert!(
            Instant::now() < deadline,
            "CLI waited for stdin after SIGINT"
        );
        thread::sleep(Duration::from_millis(5));
    }
    process.finish();
}
