use crate::{CancellationToken, Error, Result, Tool, ToolDefinition, MAX_TOOL_OUTPUT_BYTES};
use serde::Deserialize;
use serde_json::json;
use std::{collections::BTreeSet, io, process::Stdio};
use tokio::{
    io::AsyncReadExt,
    process::{Child, Command},
};

const DENIED_WORDS: &[&str] = &[
    "rm", "rmdir", "unlink", "shred", "truncate", "dd", "mkfs", "wipefs", "fdisk", "parted",
    "sudo", "doas", "shutdown", "reboot", "poweroff", "halt", "kill", "pkill", "killall",
];

/// The coding CLI's blacklist is a guardrail, not a shell sandbox.
pub fn denied_reason(command: &str) -> Option<String> {
    let normalized: String = command
        .to_ascii_lowercase()
        .chars()
        .filter(|character| !matches!(character, '\\' | '\'' | '"'))
        .collect();
    let words: BTreeSet<&str> = normalized
        .split(|character: char| {
            !(character.is_ascii_alphanumeric() || matches!(character, '_' | '.' | '-'))
        })
        .filter(|word| !word.is_empty())
        .collect();
    if let Some(word) = words
        .iter()
        .find(|word| DENIED_WORDS.contains(word) || word.starts_with("mkfs."))
    {
        return Some(format!("'{word}' is denied"));
    }
    if words.contains("find") && words.contains("-delete") {
        return Some("'find -delete' is denied".into());
    }
    if words.contains("git")
        && (words.contains("clean")
            || words.contains("restore")
            || (words.contains("reset") && words.contains("--hard"))
            || (words.contains("checkout") && words.contains("--")))
    {
        return Some("destructive git command is denied".into());
    }
    if normalized
        .split_whitespace()
        .collect::<String>()
        .contains(":(){:|:&};:")
    {
        return Some("fork bomb is denied".into());
    }
    None
}

pub fn bash_tool() -> Result<Tool> {
    #[derive(Deserialize)]
    struct Arguments {
        command: String,
    }
    Tool::new(
        ToolDefinition {
            name: "bash".into(),
            description: "Run a non-interactive bash command in the current directory.".into(),
            parameters: json!({"type":"object","properties":{"command":{"type":"string","minLength":1}},
            "required":["command"],"additionalProperties":false}),
        },
        |arguments, cancellation| async move {
            let arguments: Arguments = serde_json::from_value(arguments)
                .map_err(|error| Error::Tool(error.to_string()))?;
            run_bash(&arguments.command, cancellation).await
        },
    )
}

/// Capture bounded output. Cancellation and dropped futures stop the process
/// group on Unix; other platforms stop the direct child.
pub async fn run_bash(command: &str, cancellation: CancellationToken) -> Result<String> {
    if cancellation.is_cancelled() {
        return Err(Error::Cancelled);
    }
    if let Some(reason) = denied_reason(command) {
        return Ok(format!(
            "exit_code: 126\nstdout:\n\nstderr:\nblocked: {reason}\n"
        ));
    }
    let mut command_builder = Command::new("bash");
    command_builder
        .args(["-c", command])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(unix)]
    command_builder.process_group(0);
    let mut child = command_builder.spawn().map_err(tool_error)?;
    let mut group = ProcessGroup::new(&child);
    let mut stdout = child.stdout.take().expect("stdout was piped");
    let mut stderr = child.stderr.take().expect("stderr was piped");
    let (mut output, mut diagnostics) = (Vec::new(), Vec::new());
    let (mut out_buffer, mut err_buffer) = ([0; 8192], [0; 8192]);
    let (mut out_open, mut err_open) = (true, true);
    let (mut stopped, mut overflow) = (false, false);
    let mut total = 0;
    let mut status = None;
    // Keep the group alive through pipe cleanup: a shell can exit while a
    // background descendant still owns stdout or stderr.
    while status.is_none() || out_open || err_open {
        tokio::select! {
            biased;
            _ = cancellation.cancelled(), if !stopped => {
                group.stop(&mut child).map_err(tool_error)?;
                stopped = true;
            }
            result = stdout.read(&mut out_buffer), if out_open => {
                let count = result.map_err(tool_error)?;
                out_open = count != 0;
                overflow |= !append_output(&mut output, &mut total, &out_buffer[..count]);
            }
            result = stderr.read(&mut err_buffer), if err_open => {
                let count = result.map_err(tool_error)?;
                err_open = count != 0;
                overflow |= !append_output(&mut diagnostics, &mut total, &err_buffer[..count]);
            }
            result = child.wait(), if status.is_none() => status = Some(result.map_err(tool_error)?),
        }
        if overflow && !stopped {
            group.stop(&mut child).map_err(tool_error)?;
            stopped = true;
        }
    }
    group.disarm();
    if cancellation.is_cancelled() {
        return Err(Error::Cancelled);
    }
    if overflow {
        return Err(Error::Tool("tool output exceeds 131072 UTF-8 bytes".into()));
    }
    let observation = format!(
        "exit_code: {}\nstdout:\n{}stderr:\n{}",
        status.expect("wait completed").code().unwrap_or(-1),
        String::from_utf8_lossy(&output),
        String::from_utf8_lossy(&diagnostics)
    );
    if observation.len() > MAX_TOOL_OUTPUT_BYTES {
        return Err(Error::Tool("tool output exceeds 131072 UTF-8 bytes".into()));
    }
    Ok(observation)
}

fn append_output(output: &mut Vec<u8>, total: &mut usize, bytes: &[u8]) -> bool {
    if *total + bytes.len() > MAX_TOOL_OUTPUT_BYTES {
        return false;
    }
    *total += bytes.len();
    output.extend_from_slice(bytes);
    true
}

fn tool_error(error: impl std::fmt::Display) -> Error {
    Error::Tool(error.to_string())
}

struct ProcessGroup {
    #[cfg(unix)]
    pid: Option<nix::unistd::Pid>,
}

impl ProcessGroup {
    fn new(child: &Child) -> Self {
        #[cfg(not(unix))]
        let _ = child;
        Self {
            #[cfg(unix)]
            pid: child.id().map(|id| nix::unistd::Pid::from_raw(id as i32)),
        }
    }

    fn stop(&self, child: &mut Child) -> io::Result<()> {
        #[cfg(unix)]
        {
            let _ = child;
            if let Some(pid) = self.pid {
                match nix::sys::signal::killpg(pid, nix::sys::signal::Signal::SIGKILL) {
                    Ok(()) | Err(nix::errno::Errno::ESRCH) => {}
                    Err(error) => return Err(error.into()),
                }
            }
            Ok(())
        }
        #[cfg(not(unix))]
        {
            child.start_kill()
        }
    }

    fn disarm(&mut self) {
        #[cfg(unix)]
        {
            self.pid = None;
        }
    }
}

#[cfg(unix)]
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        if let Some(pid) = self.pid {
            let _ = nix::sys::signal::killpg(pid, nix::sys::signal::Signal::SIGKILL);
        }
    }
}
