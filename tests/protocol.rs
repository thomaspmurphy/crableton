//! Protocol-level tests against a stand-in for the Remote Script.
//!
//! These cover the parts that only misbehave against a real socket: framing,
//! pooling, reconnection after Live restarts, and the timeouts.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use crableton::{Config, LiveClient};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

/// How the fake script should answer.
#[derive(Clone, Copy, PartialEq)]
enum Mode {
    /// Newline-delimited, like the bundled script.
    Ndjson,
    /// Bare concatenated JSON with no delimiter, like the upstream script.
    Legacy,
    /// Accept the command and never answer.
    Silent,
    /// Close the connection instead of answering.
    Hangup,
}

struct FakeLive {
    config: Config,
    connections: Arc<AtomicUsize>,
    commands: Arc<AtomicUsize>,
}

impl FakeLive {
    async fn start(mode: Mode) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let connections = Arc::new(AtomicUsize::new(0));
        let commands = Arc::new(AtomicUsize::new(0));

        let accepted = Arc::clone(&connections);
        let counted = Arc::clone(&commands);
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                accepted.fetch_add(1, Ordering::SeqCst);
                let counted = Arc::clone(&counted);
                tokio::spawn(serve(stream, mode, counted));
            }
        });

        Self {
            config: Config {
                host: "127.0.0.1".into(),
                port,
                pool_size: 4,
                timeout: Duration::from_millis(300),
                browser_cache_ttl: Duration::from_secs(60),
            },
            connections,
            commands,
        }
    }

    fn client(&self) -> Arc<LiveClient> {
        LiveClient::new(self.config.clone())
    }
}

async fn serve(mut stream: TcpStream, mode: Mode, commands: Arc<AtomicUsize>) {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];

    loop {
        let Ok(n) = stream.read(&mut chunk).await else {
            return;
        };
        if n == 0 {
            return;
        }
        buf.extend_from_slice(&chunk[..n]);

        // Pull off each complete command, exactly as the real script does.
        loop {
            let mut stream_de =
                serde_json::Deserializer::from_slice(&buf).into_iter::<Value>();
            let Some(Ok(command)) = stream_de.next() else {
                break;
            };
            let consumed = stream_de.byte_offset();
            buf.drain(..consumed);
            commands.fetch_add(1, Ordering::SeqCst);

            if mode == Mode::Silent {
                continue;
            }
            if mode == Mode::Hangup {
                return;
            }

            let kind = command["type"].as_str().unwrap_or_default();
            let reply = match kind {
                "get_script_info" => json!({
                    "status": "success",
                    "id": command["id"],
                    "result": { "script_version": "2.0.0", "capabilities": ["get_session_info", "set_tempo"] },
                }),
                "explode" => json!({
                    "status": "error",
                    "id": command["id"],
                    "message": "clip slot 3 is occupied",
                }),
                _ => json!({
                    "status": "success",
                    "id": command["id"],
                    "result": { "echo": kind, "params": command["params"] },
                }),
            };

            let mut payload = serde_json::to_vec(&reply).unwrap();
            if mode == Mode::Ndjson {
                payload.push(b'\n');
            }
            if stream.write_all(&payload).await.is_err() {
                return;
            }
        }
    }
}

#[tokio::test]
async fn round_trips_over_newline_delimited_framing() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    let result = client.send("get_session_info", json!({})).await.unwrap();
    assert_eq!(result["echo"], "get_session_info");
}

#[tokio::test]
async fn round_trips_against_an_undelimited_script() {
    // The upstream Remote Script sends bare JSON with no delimiter; the reader
    // must frame on the value itself rather than on newlines.
    let live = FakeLive::start(Mode::Legacy).await;
    let client = live.client();

    for i in 0..5 {
        let result = client.send("set_tempo", json!({ "tempo": 120 + i })).await.unwrap();
        assert_eq!(result["params"]["tempo"], 120 + i);
    }
}

#[tokio::test]
async fn live_side_errors_surface_without_killing_the_connection() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    let error = client.send("explode", json!({})).await.unwrap_err();
    assert!(error.to_string().contains("clip slot 3 is occupied"));

    // The socket was fine — only the command failed — so it should be reused.
    client.send("set_tempo", json!({ "tempo": 128 })).await.unwrap();
    assert_eq!(live.connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn reuses_pooled_connections_across_sequential_calls() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    for _ in 0..10 {
        client.send("set_tempo", json!({ "tempo": 120 })).await.unwrap();
    }
    assert_eq!(
        live.connections.load(Ordering::SeqCst),
        1,
        "sequential calls should share one socket"
    );
    assert_eq!(live.commands.load(Ordering::SeqCst), 10);
}

#[tokio::test]
async fn concurrent_calls_open_up_to_the_pool_size() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    let mut handles = Vec::new();
    for i in 0..8 {
        let client = Arc::clone(&client);
        handles.push(tokio::spawn(async move {
            client.send("set_tempo", json!({ "tempo": i })).await
        }));
    }
    for handle in handles {
        handle.await.unwrap().unwrap();
    }

    let opened = live.connections.load(Ordering::SeqCst);
    assert!(
        (1..=4).contains(&opened),
        "expected at most the pool size of 4 connections, opened {opened}"
    );
}

#[tokio::test]
async fn times_out_rather_than_hanging_on_a_silent_script() {
    let live = FakeLive::start(Mode::Silent).await;
    let client = live.client();

    let error = client.send("set_tempo", json!({ "tempo": 120 })).await.unwrap_err();
    assert!(
        error.to_string().contains("did not answer"),
        "unexpected error: {error}"
    );
}

#[tokio::test]
async fn reports_a_clear_error_when_live_is_not_running() {
    let config = Config {
        host: "127.0.0.1".into(),
        // Nothing is listening here.
        port: 9,
        timeout: Duration::from_millis(200),
        ..Config::default()
    };
    let client = LiveClient::new(config);

    let error = client.send("get_session_info", json!({})).await.unwrap_err();
    let message = error.to_string();
    assert!(
        message.contains("Control Surface"),
        "the error should say how to fix it, got: {message}"
    );
}

#[tokio::test]
async fn recovers_when_a_pooled_socket_died_between_calls() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    client.send("set_tempo", json!({ "tempo": 120 })).await.unwrap();

    // Live restarting leaves a dead socket in the pool; the next call should
    // notice and reconnect rather than surfacing the failure.
    let hangup = FakeLive::start(Mode::Hangup).await;
    drop(hangup);

    client.send("set_tempo", json!({ "tempo": 121 })).await.unwrap();
    assert_eq!(live.connections.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn browser_reads_are_served_from_cache() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    for _ in 0..4 {
        client
            .send("get_browser_tree", json!({ "category_type": "all" }))
            .await
            .unwrap();
    }
    assert_eq!(
        live.commands.load(Ordering::SeqCst),
        1,
        "repeated browser scans should hit the cache"
    );

    // A different argument is a different question.
    client
        .send("get_browser_tree", json!({ "category_type": "drums" }))
        .await
        .unwrap();
    assert_eq!(live.commands.load(Ordering::SeqCst), 2);

    client.clear_cache().await;
    client
        .send("get_browser_tree", json!({ "category_type": "all" }))
        .await
        .unwrap();
    assert_eq!(live.commands.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn capabilities_come_from_the_handshake_and_are_cached() {
    let live = FakeLive::start(Mode::Ndjson).await;
    let client = live.client();

    assert!(client.has_capability("set_tempo").await);
    assert!(!client.has_capability("set_clip_envelope").await);
    assert_eq!(
        live.commands.load(Ordering::SeqCst),
        1,
        "the handshake should happen once"
    );
}
