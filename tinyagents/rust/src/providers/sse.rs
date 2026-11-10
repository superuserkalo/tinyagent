use super::{provider_error, Bytes};
use crate::{Error, Result, MAX_JSONL_LINE_BYTES};
use futures_util::StreamExt;
use serde_json::Value;

pub(super) struct Reader {
    bytes: Bytes,
    chunk: Vec<u8>,
    offset: usize,
    data: Vec<u8>,
    has_data: bool,
    ended: bool,
}

impl Reader {
    pub fn new(bytes: Bytes) -> Self {
        Self {
            bytes,
            chunk: Vec::new(),
            offset: 0,
            data: Vec::new(),
            has_data: false,
            ended: false,
        }
    }

    async fn line(&mut self) -> Result<Option<Vec<u8>>> {
        let mut line = Vec::new();
        loop {
            if self.offset == self.chunk.len() {
                let Some(chunk) = self.bytes.next().await else {
                    return Ok((!line.is_empty()).then_some(line));
                };
                self.chunk = chunk?;
                self.offset = 0;
            }
            let remaining = &self.chunk[self.offset..];
            let newline = remaining.iter().position(|byte| *byte == b'\n');
            let count = newline.unwrap_or(remaining.len());
            if line.len() + count > MAX_JSONL_LINE_BYTES {
                return Err(Error::Provider(
                    "Provider SSE line exceeds its byte limit".into(),
                ));
            }
            line.extend_from_slice(&remaining[..count]);
            self.offset += count;
            if newline.is_some() {
                self.offset += 1;
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return Ok(Some(line));
            }
        }
    }

    pub async fn next(&mut self) -> Result<Option<Value>> {
        while !self.ended {
            let line = self.line().await?;
            if let Some(line) = &line {
                std::str::from_utf8(line).map_err(provider_error)?;
                if !line.is_empty() {
                    if let Some(value) = line.strip_prefix(b"data:") {
                        let value = value.strip_prefix(b" ").unwrap_or(value);
                        let separator = usize::from(self.has_data);
                        if self.data.len() + separator + value.len() > MAX_JSONL_LINE_BYTES {
                            return Err(Error::Provider(
                                "Provider SSE event exceeds its byte limit".into(),
                            ));
                        }
                        if self.has_data {
                            self.data.push(b'\n');
                        }
                        self.has_data = true;
                        self.data.extend_from_slice(value);
                    }
                    continue;
                }
            } else {
                self.ended = true;
            }
            if self.has_data {
                let data = std::mem::take(&mut self.data);
                self.has_data = false;
                if data == b"[DONE]" {
                    self.ended = true;
                    return Ok(None);
                }
                return serde_json::from_slice(&data)
                    .map(Some)
                    .map_err(provider_error);
            }
        }
        Ok(None)
    }
}
