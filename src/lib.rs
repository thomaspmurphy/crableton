//! A fast MCP server for Ableton Live.
//!
//! Live only loads Python control surfaces, so the split is: a Python Remote
//! Script inside Live exposes its object model over a TCP socket, and this
//! crate is the MCP server that drives it.

pub mod analysis;
pub mod config;
pub mod devices;
pub mod error;
pub mod install;
pub mod live;
pub mod lom;
pub mod mcp;
pub mod tools;

pub use config::Config;
pub use error::{Error, Result};
pub use live::LiveClient;
pub use mcp::{AbletonServer, ToolRegistry};

/// The Remote Script shipped with this binary, and the version `crableton
/// install` writes into Live's User Library.
pub const REMOTE_SCRIPT: &str = include_str!("../remote_script/Crableton/__init__.py");
pub const REMOTE_SCRIPT_VERSION: &str = "2.0.0";
