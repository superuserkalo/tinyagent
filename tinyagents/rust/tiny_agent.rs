use std::{env, process::ExitCode, time::Duration};
use tiny_agent::{cli, CancellationToken, Error, Result};
use tokio::io::AsyncWriteExt;
#[cfg(unix)]
use tokio::signal::unix::{signal, SignalKind};

async fn watch_signals(cancellation: CancellationToken) -> Result<tokio::task::JoinHandle<()>> {
    #[cfg(unix)]
    {
        let mut interrupt =
            signal(SignalKind::interrupt()).map_err(|error| Error::Consumer(error.to_string()))?;
        let mut terminate =
            signal(SignalKind::terminate()).map_err(|error| Error::Consumer(error.to_string()))?;
        Ok(tokio::spawn(async move {
            tokio::select! {
                _ = interrupt.recv() => {}
                _ = terminate.recv() => {}
            }
            cancellation.cancel();
        }))
    }
    #[cfg(not(unix))]
    {
        Ok(tokio::spawn(async move {
            let _ = tokio::signal::ctrl_c().await;
            cancellation.cancel();
        }))
    }
}

fn main() -> ExitCode {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("error: {error}");
            return ExitCode::FAILURE;
        }
    };
    let result = runtime.block_on(async {
        let cancellation = CancellationToken::new();
        let signals = watch_signals(cancellation.clone()).await?;
        let args: Vec<String> = env::args().skip(1).collect();
        let result = cli::run(
            &args,
            tokio::io::stdin(),
            tokio::io::stdout(),
            tokio::io::stderr(),
            cancellation,
        )
        .await;
        signals.abort();
        result
    });
    if let Err(error) = &result {
        runtime.block_on(async {
            let mut diagnostics = tokio::io::stderr();
            let _ = tokio::time::timeout(Duration::from_secs(1), async {
                diagnostics
                    .write_all(format!("error: {error}\n").as_bytes())
                    .await?;
                diagnostics.flush().await
            })
            .await;
        });
    }
    // Tokio stdio uses blocking workers. The adapter has settled its run;
    // process exit must not wait for another keystroke or a blocked output pipe.
    runtime.shutdown_background();
    if result.is_ok() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
