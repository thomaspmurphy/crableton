use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use rmcp::ServiceExt;
use rmcp::transport::stdio;

use crableton::{AbletonServer, Config, LiveClient, ToolRegistry, install, tools};

#[derive(Parser)]
#[command(
    name = "crableton",
    version,
    about = "A fast MCP server for Ableton Live"
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Serve MCP over stdio. This is what an MCP client runs. (Default.)
    Serve,

    /// Install the bundled Remote Script into Ableton's User Library.
    Install {
        /// Path to the User Library, if it cannot be found automatically.
        #[arg(long)]
        user_library: Option<PathBuf>,
    },

    /// Check that Live is reachable and report what it is running.
    Doctor,

    /// List the tools this server exposes.
    Tools {
        /// Print the full JSON schema of every tool.
        #[arg(long)]
        schema: bool,
    },
}

fn main() -> Result<()> {
    // Logs must never touch stdout: that is the MCP transport.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "crableton=info".into()),
        )
        .init();

    let cli = Cli::parse();
    let registry = || {
        Arc::new(ToolRegistry::new(tools::selected(
            std::env::var("CRABLETON_TOOLSETS").ok().as_deref(),
        )))
    };

    match cli.command.unwrap_or(Command::Serve) {
        Command::Serve => runtime()?.block_on(serve(registry())),
        Command::Doctor => runtime()?.block_on(doctor(registry())),
        Command::Install { user_library } => run_install(user_library.as_deref()),
        Command::Tools { schema } => list_tools(&registry(), schema),
    }
}

fn runtime() -> Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .context("could not start the async runtime")
}

async fn serve(tools: Arc<ToolRegistry>) -> Result<()> {
    let config = Config::from_env();
    tracing::info!(
        addr = %config.addr(),
        pool = config.pool_size,
        tools = tools.len(),
        "crableton starting"
    );

    let live = LiveClient::new(config);

    // Connect eagerly so the log says plainly whether Live is there, but do not
    // make it fatal: a client may start this server long before Live is open.
    match live.script_info().await {
        Ok(info) => tracing::info!(
            version = %info.get("script_version").and_then(|v| v.as_str()).unwrap_or("unknown"),
            "connected to the Crableton Remote Script"
        ),
        Err(e) => tracing::warn!(error = %e, "not connected to Live yet; will retry per request"),
    }

    let service = AbletonServer::new(Arc::clone(&live), tools)
        .serve(stdio())
        .await
        .context("could not start the MCP stdio transport")?;

    service.waiting().await?;
    live.disconnect().await;
    Ok(())
}

async fn doctor(tools: Arc<ToolRegistry>) -> Result<()> {
    let config = Config::from_env();
    println!("crableton {}", env!("CARGO_PKG_VERSION"));
    println!("  tools exposed:  {}", tools.len());
    println!("  Live address:   {}", config.addr());

    match install::locate_user_library() {
        Some(path) => {
            let script = path.join("Remote Scripts").join(install::SCRIPT_FOLDER).join("__init__.py");
            println!("  User Library:   {}", path.display());
            println!(
                "  Remote Script:  {}",
                if script.exists() {
                    format!("installed at {}", script.display())
                } else {
                    "not installed, run `crableton install`".to_string()
                }
            );
        }
        None => println!("  User Library:   not found"),
    }

    let live = LiveClient::new(config);
    match live.script_info().await {
        Ok(info) => {
            println!("  Connection:     OK");
            println!(
                "  Script version: {} (this binary bundles {})",
                info.get("script_version").and_then(|v| v.as_str()).unwrap_or("unknown"),
                crableton::REMOTE_SCRIPT_VERSION
            );
            let commands = info
                .get("capabilities")
                .and_then(|c| c.as_array())
                .map_or(0, Vec::len);
            println!("  Live commands:  {commands}");

            match live.send("get_session_info", serde_json::json!({})).await {
                Ok(session) => println!(
                    "  Open set:       {} tracks at {} BPM",
                    session.get("track_count").and_then(|v| v.as_u64()).unwrap_or(0),
                    session.get("tempo").and_then(|v| v.as_f64()).unwrap_or(0.0),
                ),
                Err(e) => println!("  Open set:       could not read ({e})"),
            }
        }
        Err(e) => {
            println!("  Connection:     FAILED");
            println!("  {e}");
        }
    }

    live.disconnect().await;
    Ok(())
}

fn run_install(user_library: Option<&std::path::Path>) -> Result<()> {
    let installed = install::install(user_library)?;
    match &installed.replaced_version {
        Some(old) if old == crableton::REMOTE_SCRIPT_VERSION => {
            println!("Remote Script already at {old}; rewrote it anyway.");
        }
        Some(old) => println!("Updated the Remote Script from {old} to {}.", crableton::REMOTE_SCRIPT_VERSION),
        None => println!("Installed Remote Script {}.", crableton::REMOTE_SCRIPT_VERSION),
    }
    println!("  {}", installed.path.display());
    println!();
    println!("Now, in Ableton Live:");
    println!("  1. Restart Live so it picks up the script.");
    println!("  2. Settings > Link, Tempo & MIDI > Control Surface: choose \"Crableton\".");
    println!("  3. Run `crableton doctor` to confirm the connection.");
    Ok(())
}

fn list_tools(tools: &ToolRegistry, schema: bool) -> Result<()> {
    if schema {
        let all: Vec<_> = tools
            .all()
            .iter()
            .map(|t| {
                serde_json::json!({
                    "name": t.name,
                    "group": t.group,
                    "command": t.command,
                    "description": t.description,
                    "readOnly": t.read_only,
                    "destructive": t.destructive,
                    "inputSchema": t.input_schema(),
                })
            })
            .collect();
        println!("{}", serde_json::to_string_pretty(&all)?);
        return Ok(());
    }

    let mut group = "";
    for tool in tools.all() {
        if tool.group != group {
            group = tool.group;
            println!("\n{group}");
        }
        let marker = if tool.read_only { " " } else if tool.destructive { "!" } else { "*" };
        let summary = tool.description.split(['.', '\n']).next().unwrap_or("");
        println!("  {marker} {:<32} {summary}.", tool.name);
    }
    println!("\n  (blank = read-only, * = modifies the set, ! = can discard work)");
    println!("  {} tools total", tools.len());
    Ok(())
}
