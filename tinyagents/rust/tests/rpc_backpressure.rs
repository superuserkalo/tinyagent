#![cfg(unix)]

use nix::{
    sys::signal::{kill, Signal},
    unistd::Pid,
};
use serde_json::json;
use std::{
    io::{Read, Write},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

struct ChildGuard(Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn real_cli_stalled_stdout_exits_on_eof_and_interrupt() {
    for interrupt in [false, true] {
        let mut child = ChildGuard(
            Command::new(env!("CARGO_BIN_EXE_tiny-agent"))
                .args(["--mode", "rpc"])
                .env_remove("OPENAI_API_KEY")
                .env_remove("ANTHROPIC_API_KEY")
                .env("TINYAGENT_PROVIDER", "openai")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .unwrap(),
        );
        let mut input = child.0.stdin.take().unwrap();
        let mut output = child.0.stdout.take().unwrap();
        let mut diagnostics = child.0.stderr.take().unwrap();
        let command = format!(
            "{}\n",
            json!({"type":"get_state","id":"x".repeat(256 * 1024)})
        );
        input.write_all(command.as_bytes()).unwrap();
        // Receiving the first byte proves signal registration and command entry.
        // Keep the rest of stdout unread so the native pipe stays full.
        output.read_exact(&mut [0]).unwrap();
        if interrupt {
            kill(Pid::from_raw(child.0.id() as i32), Signal::SIGINT).unwrap();
        } else {
            drop(input);
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        let status = loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                break status;
            }
            assert!(
                Instant::now() < deadline,
                "CLI waited for a stalled stdout reader"
            );
            thread::sleep(Duration::from_millis(5));
        };
        assert!(
            !status.success(),
            "incomplete output was reported as success"
        );
        let mut text = String::new();
        diagnostics.read_to_string(&mut text).unwrap();
        assert!(
            text.contains("RPC output did not drain after cancellation"),
            "{text}"
        );
    }
}
