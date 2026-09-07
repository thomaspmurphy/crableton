use std::time::Duration;

/// Everything that can go wrong between the MCP server and Ableton Live.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("could not reach Ableton Live at {addr}: {source}. Is Live running with the Crableton Control Surface selected in Settings > Link/Tempo/MIDI?")]
    Connect {
        addr: String,
        #[source]
        source: std::io::Error,
    },

    #[error("connection to Ableton Live was lost: {0}")]
    Disconnected(String),

    #[error("Ableton Live did not answer `{command}` within {}s", timeout.as_secs_f32())]
    Timeout { command: String, timeout: Duration },

    /// The Remote Script ran the command and reported a failure. This is a
    /// user-facing problem (bad index, occupied slot, ...), not a bug.
    #[error("{0}")]
    Live(String),

    #[error("malformed response from the Remote Script: {0}")]
    Protocol(String),

    #[error("unknown tool `{0}`")]
    UnknownTool(String),

    #[error("invalid arguments for `{tool}`: {reason}")]
    BadArguments { tool: String, reason: String },

    #[error(transparent)]
    Io(#[from] std::io::Error),
}

impl Error {
    /// Whether the socket is unusable and must not be returned to the pool.
    pub fn is_connection_level(&self) -> bool {
        matches!(
            self,
            Error::Connect { .. } | Error::Disconnected(_) | Error::Timeout { .. } | Error::Io(_)
        )
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
