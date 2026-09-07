use std::time::Duration;

/// Runtime knobs, all overridable from the environment so the server can be
/// configured from an MCP client's `env` block.
#[derive(Debug, Clone)]
pub struct Config {
    pub host: String,
    pub port: u16,
    /// How many sockets we keep open to the Remote Script. The script handles
    /// one command at a time per connection, so this is what lets a slow
    /// read (a browser scan) run alongside a quick `set_tempo`.
    pub pool_size: usize,
    /// Default per-command budget; some commands override it upwards.
    pub timeout: Duration,
    /// How long a browser listing stays fresh. Scanning Live's browser is by
    /// far the slowest call in the protocol and its content only changes when
    /// the user installs packs.
    pub browser_cache_ttl: Duration,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_string(),
            // The bundled Crableton script listens here. Set ABLETON_PORT=9877
            // to drive the upstream ableton-mcp script instead.
            port: 9878,
            pool_size: 4,
            timeout: Duration::from_secs(15),
            browser_cache_ttl: Duration::from_secs(120),
        }
    }
}

impl Config {
    pub fn from_env() -> Self {
        let d = Self::default();
        Self {
            host: env_str("ABLETON_HOST").unwrap_or(d.host),
            port: env_parse("ABLETON_PORT").unwrap_or(d.port),
            pool_size: env_parse("CRABLETON_POOL_SIZE").unwrap_or(d.pool_size).clamp(1, 16),
            timeout: env_parse::<u64>("CRABLETON_TIMEOUT_MS")
                .map(Duration::from_millis)
                .unwrap_or(d.timeout),
            browser_cache_ttl: env_parse::<u64>("CRABLETON_BROWSER_CACHE_SECS")
                .map(Duration::from_secs)
                .unwrap_or(d.browser_cache_ttl),
        }
    }

    pub fn addr(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

fn env_str(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.trim().is_empty())
}

fn env_parse<T: std::str::FromStr>(key: &str) -> Option<T> {
    env_str(key).and_then(|v| v.trim().parse().ok())
}
