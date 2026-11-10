use crate::MAX_JSONL_LINE_BYTES;
use std::io;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};

pub(crate) enum LineError {
    Io(io::Error),
    TooLarge,
    InvalidUtf8,
}

/// The partial line lives in the reader so select! can cancel next() safely.
pub(crate) struct Lines<R> {
    reader: BufReader<R>,
    partial: Vec<u8>,
}

impl<R: AsyncRead + Unpin> Lines<R> {
    pub fn new(reader: R) -> Self {
        Self {
            reader: BufReader::new(reader),
            partial: Vec::new(),
        }
    }

    pub async fn next(&mut self) -> Result<Option<String>, LineError> {
        loop {
            let available = self.reader.fill_buf().await.map_err(LineError::Io)?;
            let eof = available.is_empty();
            if eof && self.partial.is_empty() {
                return Ok(None);
            }
            let newline = available.iter().position(|byte| *byte == b'\n');
            let count = newline.unwrap_or(available.len());
            if self.partial.len() + count > MAX_JSONL_LINE_BYTES {
                return Err(LineError::TooLarge);
            }
            self.partial.extend_from_slice(&available[..count]);
            self.reader.consume(count + usize::from(newline.is_some()));
            if newline.is_some() || eof {
                return String::from_utf8(std::mem::take(&mut self.partial))
                    .map(Some)
                    .map_err(|_| LineError::InvalidUtf8);
            }
        }
    }
}
