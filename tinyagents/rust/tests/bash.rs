use std::time::{Duration, Instant};
use tiny_agent::{denied_reason, run_bash, CancellationToken, Error};

#[tokio::test]
async fn blacklist_output_and_nonzero_exit() {
    assert!(denied_reason("printf 'hello\\n'").is_none());
    for command in [
        "rm -rf /",
        "r\"\"m -rf /",
        "sudo echo hi",
        "git reset --hard HEAD",
        "find . -delete",
        "mkfs.ext4 /dev/sda",
    ] {
        assert!(denied_reason(command).is_some(), "{command}");
    }
    let result = run_bash(
        "printf out; printf err >&2; exit 7",
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(result, "exit_code: 7\nstdout:\noutstderr:\nerr");
    let blocked = run_bash("sudo echo hi", CancellationToken::new())
        .await
        .unwrap();
    assert!(blocked.starts_with("exit_code: 126"));
}

#[tokio::test]
async fn output_limit_stops_process() {
    let result = tokio::time::timeout(
        Duration::from_secs(2),
        run_bash(
            "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done",
            CancellationToken::new(),
        ),
    )
    .await
    .unwrap();
    assert!(matches!(result, Err(Error::Tool(message)) if message.contains("131072")));
}

#[cfg(unix)]
#[tokio::test]
async fn cancellation_includes_descendants_after_shell_exit() {
    for command in ["sleep 3", "sleep 3 &"] {
        let cancellation = CancellationToken::new();
        let start = Instant::now();
        let (result, ()) = tokio::join!(run_bash(command, cancellation.clone()), async {
            tokio::time::sleep(Duration::from_millis(100)).await;
            cancellation.cancel();
        });
        assert_eq!(result.unwrap_err(), Error::Cancelled);
        assert!(
            start.elapsed() < Duration::from_secs(2),
            "{command} left a child holding its pipes"
        );
    }
}
