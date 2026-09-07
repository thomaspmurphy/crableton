use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::Duration;

/// A command sent to the Remote Script.
///
/// `id` is an extension: the bundled script echoes it back so we can assert we
/// are reading our own reply, older scripts simply ignore it.
#[derive(Debug, Serialize)]
pub struct Command<'a> {
    #[serde(rename = "type")]
    pub kind: &'a str,
    pub params: &'a Value,
    pub id: u64,
}

#[derive(Debug, Deserialize)]
pub struct Response {
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub result: Value,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    pub id: Option<u64>,
}

impl Response {
    pub fn is_error(&self) -> bool {
        self.status == "error"
    }

    pub fn error_message(&self) -> String {
        self.message
            .clone()
            .unwrap_or_else(|| "unknown error from Ableton Live".to_string())
    }
}

/// Commands whose work on Live's main thread legitimately takes longer than the
/// default budget. Everything else uses [`Config::timeout`](crate::Config).
pub fn timeout_for(command: &str, default: Duration) -> Duration {
    let secs = match command {
        // Decodes and imports an audio file on the main thread.
        "create_audio_clip" => 90,
        // Walks Live's whole browser tree; seconds on a large library.
        "get_browser_tree" | "search_browser" => 60,
        "get_browser_items_at_path" | "load_browser_item" | "load_instrument_or_effect" => 30,
        // Serialises every track, clip and parameter in the set.
        "get_session_snapshot" => 45,
        // A batch is N commands in one main-thread pass.
        "batch" => 120,
        _ => return default,
    };
    Duration::from_secs(secs).max(default)
}

/// Read-only listings worth caching between calls.
pub fn is_cacheable(command: &str) -> bool {
    matches!(
        command,
        "get_browser_tree" | "get_browser_items_at_path" | "search_browser"
    )
}
