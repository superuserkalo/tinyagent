#![cfg(unix)]

use nix::{
    errno::Errno,
    libc,
    sys::signal::{kill, Signal},
    unistd::{pipe, Pid},
};
use std::{
    fs::File,
    io::{BufRead, Read},
    os::fd::{AsRawFd, OwnedFd},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::Command,
    time::{sleep, timeout},
};

fn fill_pipe(write: &OwnedFd) {
    // Only this test writes while the child waits at its initial prompt. Restore
    // the shared file flags before allowing the child to attempt its next write.
    let flags = unsafe { libc::fcntl(write.as_raw_fd(), libc::F_GETFL) };
    assert_ne!(flags, -1);
    assert_ne!(
        unsafe { libc::fcntl(write.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) },
        -1
    );
    let result = loop {
        match nix::unistd::write(write, &[b'x'; 4096]) {
            Ok(_) => continue,
            Err(Errno::EINTR) => continue,
            Err(error) => break error,
        }
    };
    // A large atomic write can return EAGAIN with a few bytes still free.
    let last = loop {
        match nix::unistd::write(write, b"x") {
            Ok(_) => continue,
            Err(Errno::EINTR) => continue,
            Err(error) => break error,
        }
    };
    assert_ne!(
        unsafe { libc::fcntl(write.as_raw_fd(), libc::F_SETFL, flags) },
        -1
    );
    assert_eq!(result, Errno::EAGAIN);
    assert_eq!(last, Errno::EAGAIN);
}

async fn interrupt_with_stalled_output(stderr: bool) {
    let (read, write) = pipe().unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_tiny-agent"))
        .env_remove("OPENAI_API_KEY")
        .env_remove("ANTHROPIC_API_KEY")
        .env("TINYAGENT_PROVIDER", "openai")
        .env("TINYAGENT_MODEL", "fixture")
        .stdin(Stdio::piped())
        .stdout(if stderr {
            Stdio::piped()
        } else {
            Stdio::from(write.try_clone().unwrap())
        })
        .stderr(if stderr {
            Stdio::from(write.try_clone().unwrap())
        } else {
            Stdio::piped()
        })
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    let mut held_reader = Some(std::io::BufReader::new(File::from(read)));
    let mut banner = Vec::new();
    let drain = if stderr {
        let mut output = BufReader::new(child.stdout.take().unwrap());
        timeout(Duration::from_secs(3), output.read_until(b'>', &mut banner))
            .await
            .unwrap()
            .unwrap();
        output.read_exact(&mut [0]).await.unwrap();
        Some(tokio::spawn(async move {
            tokio::io::copy(&mut output, &mut tokio::io::sink()).await
        }))
    } else {
        let mut reader = held_reader.take().unwrap();
        let read_banner = tokio::task::spawn_blocking(move || {
            let mut banner = Vec::new();
            reader.read_until(b'>', &mut banner).unwrap();
            reader.read_exact(&mut [0]).unwrap();
            (reader, banner)
        });
        let (reader, text) = timeout(Duration::from_secs(3), read_banner)
            .await
            .unwrap()
            .unwrap();
        held_reader = Some(reader);
        banner = text;
        None
    };
    assert!(banner.starts_with(b"TinyAgent."));
    fill_pipe(&write);
    input
        .write_all(if stderr { b"x\n" } else { b"\n" })
        .await
        .unwrap();
    sleep(Duration::from_millis(100)).await;
    kill(Pid::from_raw(child.id().unwrap() as i32), Signal::SIGINT).unwrap();
    let result = timeout(Duration::from_secs(3), child.wait()).await;
    if result.is_err() {
        child.kill().await.unwrap();
        child.wait().await.unwrap();
    }
    if let Some(drain) = drain {
        drain.await.unwrap().unwrap();
    }
    drop(held_reader);
    drop(write);
    let status = result
        .expect(if stderr {
            "CLI hung on stderr after cancellation"
        } else {
            "CLI hung on stdout after cancellation"
        })
        .unwrap();
    assert!(
        !status.success(),
        "incomplete console output was reported as success"
    );
}

#[tokio::test]
async fn interrupt_exits_with_stalled_stdout() {
    interrupt_with_stalled_output(false).await;
}

#[tokio::test]
async fn interrupt_exits_with_stalled_stderr() {
    interrupt_with_stalled_output(true).await;
}
