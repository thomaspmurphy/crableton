use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{Mutex, RwLock, Semaphore};

use super::protocol::{Command, Response, is_cacheable, timeout_for};
use crate::config::Config;
use crate::error::{Error, Result};

/// One socket to the Remote Script, plus whatever bytes we have read past the
/// end of the last response.
struct Conn {
    stream: TcpStream,
    buf: Vec<u8>,
    /// Fresh connections have never carried a command, so a failure on one is a
    /// real problem rather than a stale-socket hiccup worth retrying.
    fresh: bool,
}

impl Conn {
    async fn connect(cfg: &Config) -> Result<Self> {
        let addr = cfg.addr();
        let stream = tokio::time::timeout(Duration::from_secs(5), TcpStream::connect(&addr))
            .await
            .map_err(|_| Error::Connect {
                addr: addr.clone(),
                source: std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "connect timed out after 5s",
                ),
            })?
            .map_err(|source| Error::Connect { addr, source })?;
        // Commands are small and latency matters far more than packet count.
        let _ = stream.set_nodelay(true);
        Ok(Self {
            stream,
            buf: Vec::with_capacity(16 * 1024),
            fresh: true,
        })
    }

    async fn roundtrip(&mut self, command: &str, id: u64, params: &Value) -> Result<Value> {
        let payload = serde_json::to_vec(&Command {
            kind: command,
            params,
            id,
        })
        .map_err(|e| Error::Protocol(format!("could not encode `{command}`: {e}")))?;

        // The trailing newline lets the bundled script frame on lines; the
        // upstream script parses the whole buffer as JSON and tolerates it as
        // trailing whitespace, so this stays compatible either way.
        self.stream.write_all(&payload).await?;
        self.stream.write_all(b"\n").await?;
        self.stream.flush().await?;
        self.fresh = false;

        loop {
            let response = self.read_response().await?;
            // A reply to a request we already gave up on (timeout) can still be
            // sitting in the socket. Skip it rather than mis-attributing it.
            if let Some(got) = response.id
                && got != id
            {
                tracing::debug!(expected = id, got, "discarding stale response");
                continue;
            }
            if response.is_error() {
                return Err(Error::Live(response.error_message()));
            }
            return Ok(response.result);
        }
    }

    /// Pull one JSON value off the wire.
    ///
    /// Deliberately framing-agnostic: it parses the longest valid JSON prefix of
    /// the buffer, which works for the bundled script's newline-delimited
    /// replies and for the upstream script's bare concatenated objects alike.
    async fn read_response(&mut self) -> Result<Response> {
        let mut chunk = [0u8; 16 * 1024];
        loop {
            if !self.buf.is_empty() {
                // Scoped so the borrow of `buf` ends before we drain it.
                let parsed = {
                    let mut stream =
                        serde_json::Deserializer::from_slice(&self.buf).into_iter::<Response>();
                    match stream.next() {
                        Some(Ok(response)) => Some(Ok((response, stream.byte_offset()))),
                        // Truncated value: keep reading.
                        Some(Err(e)) if e.is_eof() => None,
                        Some(Err(e)) => Some(Err(e)),
                        None => None,
                    }
                };

                match parsed {
                    Some(Ok((response, consumed))) => {
                        self.buf.drain(..consumed);
                        return Ok(response);
                    }
                    Some(Err(e)) => {
                        let preview: String = String::from_utf8_lossy(&self.buf)
                            .chars()
                            .take(200)
                            .collect();
                        self.buf.clear();
                        return Err(Error::Protocol(format!("{e} (near: {preview})")));
                    }
                    None => {}
                }
            }

            let n = self.stream.read(&mut chunk).await?;
            if n == 0 {
                return Err(Error::Disconnected(
                    "the Remote Script closed the connection".to_string(),
                ));
            }
            self.buf.extend_from_slice(&chunk[..n]);
        }
    }
}

struct CacheEntry {
    stored: Instant,
    value: Value,
}

/// Pooled, async client for the Ableton Live Remote Script.
///
/// The Remote Script serves each connection from its own thread but touches
/// Live's API on the main thread, so several sockets buy real parallelism for
/// read-only commands (browser scans, snapshots) while keeping mutations
/// ordered per connection.
pub struct LiveClient {
    cfg: Config,
    idle: Mutex<Vec<Conn>>,
    permits: Semaphore,
    next_id: AtomicU64,
    cache: Mutex<HashMap<String, CacheEntry>>,
    script_info: RwLock<Option<Value>>,
}

impl LiveClient {
    pub fn new(cfg: Config) -> Arc<Self> {
        Arc::new(Self {
            permits: Semaphore::new(cfg.pool_size),
            idle: Mutex::new(Vec::with_capacity(cfg.pool_size)),
            cfg,
            next_id: AtomicU64::new(1),
            cache: Mutex::new(HashMap::new()),
            script_info: RwLock::new(None),
        })
    }

    pub fn config(&self) -> &Config {
        &self.cfg
    }

    /// Run a command, transparently reconnecting if a pooled socket went stale
    /// (Live restarted, the Control Surface was re-selected, ...).
    pub async fn send(&self, command: &str, params: Value) -> Result<Value> {
        if is_cacheable(command)
            && let Some(hit) = self.cache_get(command, &params).await
        {
            tracing::debug!(command, "browser cache hit");
            return Ok(hit);
        }

        let result = self.send_uncached(command, &params).await?;

        if is_cacheable(command) {
            self.cache_put(command, &params, &result).await;
        }
        Ok(result)
    }

    async fn send_uncached(&self, command: &str, params: &Value) -> Result<Value> {
        let _permit = self
            .permits
            .acquire()
            .await
            .map_err(|_| Error::Disconnected("client is shutting down".to_string()))?;

        let timeout = timeout_for(command, self.cfg.timeout);

        // One retry, and only when a *reused* socket failed at the connection
        // level: that is the stale-socket case. A fresh socket failing the same
        // way means Live is genuinely unreachable.
        for attempt in 0..2 {
            let mut conn = self.take_conn().await?;
            let was_fresh = conn.fresh;
            let id = self.next_id.fetch_add(1, Ordering::Relaxed);

            match tokio::time::timeout(timeout, conn.roundtrip(command, id, params)).await {
                Ok(Ok(value)) => {
                    self.put_conn(conn).await;
                    return Ok(value);
                }
                Ok(Err(e)) => {
                    if !e.is_connection_level() {
                        // Live answered, it just said no. Socket is still good.
                        self.put_conn(conn).await;
                        return Err(e);
                    }
                    if was_fresh || attempt == 1 {
                        return Err(e);
                    }
                    tracing::debug!(command, error = %e, "retrying on a fresh connection");
                }
                Err(_) => {
                    // The reply may still be in flight; this socket is poisoned.
                    return Err(Error::Timeout {
                        command: command.to_string(),
                        timeout,
                    });
                }
            }
        }
        unreachable!("retry loop always returns")
    }

    async fn take_conn(&self) -> Result<Conn> {
        if let Some(conn) = self.idle.lock().await.pop() {
            return Ok(conn);
        }
        Conn::connect(&self.cfg).await
    }

    async fn put_conn(&self, conn: Conn) {
        let mut idle = self.idle.lock().await;
        if idle.len() < self.cfg.pool_size {
            idle.push(conn);
        }
    }

    /// Drop every pooled socket. Used by `doctor` and on shutdown.
    pub async fn disconnect(&self) {
        self.idle.lock().await.clear();
    }

    // ── Handshake ────────────────────────────────────────────────────────────

    /// Ask the Remote Script who it is. Cached for the life of the process.
    pub async fn script_info(&self) -> Result<Value> {
        if let Some(info) = self.script_info.read().await.clone() {
            return Ok(info);
        }
        let info = self.send_uncached("get_script_info", &Value::Null).await?;
        *self.script_info.write().await = Some(info.clone());
        Ok(info)
    }

    /// Whether the connected script advertises a given command.
    ///
    /// Scripts predating the capability list report nothing, in which case we
    /// let the call through and surface the script's own "unknown command"
    /// error rather than guessing.
    pub async fn has_capability(&self, name: &str) -> bool {
        let Ok(info) = self.script_info().await else {
            return true;
        };
        match info.get("capabilities").and_then(|c| c.as_array()) {
            Some(caps) => caps.iter().any(|c| c.as_str() == Some(name)),
            None => true,
        }
    }

    // ── Browser cache ────────────────────────────────────────────────────────

    fn cache_key(command: &str, params: &Value) -> String {
        format!("{command}\u{1}{params}")
    }

    async fn cache_get(&self, command: &str, params: &Value) -> Option<Value> {
        let key = Self::cache_key(command, params);
        let mut cache = self.cache.lock().await;
        match cache.get(&key) {
            Some(entry) if entry.stored.elapsed() < self.cfg.browser_cache_ttl => {
                Some(entry.value.clone())
            }
            Some(_) => {
                cache.remove(&key);
                None
            }
            None => None,
        }
    }

    async fn cache_put(&self, command: &str, params: &Value, value: &Value) {
        self.cache.lock().await.insert(
            Self::cache_key(command, params),
            CacheEntry {
                stored: Instant::now(),
                value: value.clone(),
            },
        );
    }

    pub async fn clear_cache(&self) {
        self.cache.lock().await.clear();
    }
}
