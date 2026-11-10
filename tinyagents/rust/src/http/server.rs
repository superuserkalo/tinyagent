use super::{body::ConnectionContext, HttpState};
use crate::{Agent, CancellationToken, Error, Provider, Result};
use hyper::{server::conn::http1, service::service_fn};
use hyper_util::rt::{TokioIo, TokioTimer};
use std::{
    convert::Infallible,
    net::{Ipv4Addr, SocketAddr},
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::TcpListener,
    sync::watch,
};

pub struct HttpOptions {
    pub address: SocketAddr,
    pub token: String,
    pub allow_remote: bool,
}

impl Default for HttpOptions {
    fn default() -> Self {
        Self {
            address: (Ipv4Addr::LOCALHOST, 8080).into(),
            token: String::new(),
            allow_remote: false,
        }
    }
}

impl HttpOptions {
    fn validate(&self) -> Result<()> {
        if !self.token.bytes().all(|byte| (0x21..=0x7e).contains(&byte)) {
            return Err(Error::Configuration(
                "Token must contain only printable non-space ASCII characters".into(),
            ));
        }
        if !self.address.ip().is_loopback() && (!self.allow_remote || self.token.is_empty()) {
            return Err(Error::Configuration(
                "Non-loopback binding requires allow_remote and a bearer token".into(),
            ));
        }
        Ok(())
    }
}

/// Owns the listener. Close waits for cooperative run cleanup; dropping the
/// handle requests shutdown without waiting.
pub struct HttpServer {
    address: SocketAddr,
    cancellation: CancellationToken,
    done: watch::Receiver<Option<Result<()>>>,
}

impl HttpServer {
    pub fn url(&self) -> String {
        format!("http://{}", self.address)
    }

    pub async fn close(&self) -> Result<()> {
        self.cancellation.cancel();
        self.wait().await
    }

    pub async fn wait(&self) -> Result<()> {
        let mut done = self.done.clone();
        let result = done
            .wait_for(|result| result.is_some())
            .await
            .map_err(|_| Error::Consumer("HTTP server task ended unexpectedly".into()))?;
        result.as_ref().unwrap().clone()
    }
}

impl Drop for HttpServer {
    fn drop(&mut self) {
        self.cancellation.cancel();
    }
}

pub async fn serve<P: Provider + 'static>(
    agent: Arc<Agent<P>>,
    options: HttpOptions,
) -> Result<HttpServer> {
    options.validate()?;
    let listener = TcpListener::bind(options.address)
        .await
        .map_err(|error| Error::Consumer(error.to_string()))?;
    let address = listener
        .local_addr()
        .map_err(|error| Error::Consumer(error.to_string()))?;
    let state = Arc::new(HttpState::new(agent, options.token));
    let cancellation = state.cancellation.clone();
    let (done, receiver) = watch::channel(None);
    tokio::spawn(async move {
        let result = loop {
            let accepted = tokio::select! {
                biased;
                _ = state.cancellation.cancelled() => break Ok(()),
                accepted = listener.accept() => accepted,
            };
            match accepted {
                Ok((stream, _)) => {
                    let state = state.clone();
                    state.tasks.clone().spawn(async move {
                        state.connection(stream).await;
                    });
                }
                Err(error) => break Err(Error::Consumer(error.to_string())),
            }
        };
        drop(listener);
        state.shutdown().await;
        done.send_replace(Some(result));
    });
    Ok(HttpServer {
        address,
        cancellation,
        done: receiver,
    })
}

impl<P: Provider + 'static> HttpState<P> {
    pub(super) async fn connection<I>(self: Arc<Self>, io: I)
    where
        I: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let cancellation = self.cancellation.child_token();
        let guard = cancellation.clone().drop_guard();
        let (written, receiver) = watch::channel(None);
        let context = ConnectionContext {
            cancellation: cancellation.clone(),
            written: receiver,
        };
        let state = self.clone();
        let service = service_fn(move |request| {
            let (state, context) = (state.clone(), context.clone());
            async move { Ok::<_, Infallible>(state.handle(request, context).await) }
        });
        let connection = http1::Builder::new()
            .keep_alive(false)
            .timer(TokioTimer::new())
            .header_read_timeout(Duration::from_secs(10))
            .max_buf_size(64 * 1024)
            .serve_connection(TokioIo::new(io), service);
        tokio::pin!(connection);
        let success = tokio::select! {
            result = &mut connection => result.is_ok(),
            _ = cancellation.cancelled() => {
                connection.as_mut().graceful_shutdown();
                matches!(tokio::time::timeout(Duration::from_secs(1), &mut connection).await, Ok(Ok(())))
            }
        };
        written.send_replace(Some(success));
        if success {
            guard.disarm();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exposure_requires_explicit_remote_opt_in_and_a_valid_token() {
        assert_eq!(
            HttpOptions::default().address,
            "127.0.0.1:8080".parse().unwrap()
        );
        for address in ["127.0.0.1:0", "[::1]:8080"] {
            assert!(HttpOptions {
                address: address.parse().unwrap(),
                ..HttpOptions::default()
            }
            .validate()
            .is_ok());
        }
        for address in ["0.0.0.0:8080", "[::]:8080", "192.0.2.1:8080"] {
            for (allow_remote, token, valid) in [
                (false, "", false),
                (true, "", false),
                (false, "secret", false),
                (true, "secret", true),
            ] {
                let options = HttpOptions {
                    address: address.parse().unwrap(),
                    token: token.into(),
                    allow_remote,
                };
                assert_eq!(
                    options.validate().is_ok(),
                    valid,
                    "{address}, {allow_remote}"
                );
            }
        }
        for token in ["a b", "a\nb", "é", "\u{7f}"] {
            assert!(HttpOptions {
                token: token.into(),
                ..HttpOptions::default()
            }
            .validate()
            .is_err());
        }
    }
}
