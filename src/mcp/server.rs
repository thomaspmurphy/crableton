use std::borrow::Cow;
use std::sync::Arc;

use rmcp::handler::server::ServerHandler;
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ErrorData,
    Implementation, InitializeResult, ListToolsResult, PaginatedRequestParams, ServerCapabilities,
    Tool, ToolAnnotations,
};
use rmcp::service::{RequestContext, RoleServer};
use serde_json::{Map, Value, json};

use crate::error::Error;
use crate::live::LiveClient;
use crate::mcp::registry::{ToolDef, ToolRegistry};

/// How the model is told to drive Live. Kept short, long preambles crowd out
/// the tool descriptions, which are where the real guidance lives.
const INSTRUCTIONS: &str = "\
Control Ableton Live over its Remote Script.

Conventions:
- All indices are 0-based. Times and durations are in beats, not seconds or bars.
- `track_index` addresses the regular tracks unless you pass `track_type` \
(\"return\" or \"master\"); `track_index` is ignored for the master track.
- Session clips are addressed by clip slot; arrangement clips by their position \
in the track's timeline. Pass `view: \"arrangement\"` on clip tools to target the latter.
- Note writes are additive. To edit an existing clip, read it with `get_clip_notes`, \
change the list, then call `replace_clip_notes`.
- Prefer `batch` for multi-step edits: it runs the whole sequence in a single \
pass on Live's main thread, which is both far faster and atomic from the user's view.

Start with `get_session_info` to see what you are working with.";

/// Sentinel commands for tools answered locally rather than by Live.
const LOCAL_LOM: &str = "__local_lom";
const LOCAL_DEVICE_REFERENCE: &str = "__local_device_reference";

/// Analysis tools fetch their own snapshot rather than forwarding arguments.
fn is_analysis(command: &str) -> bool {
    matches!(
        command,
        crate::tools::analysis::HARMONY
            | crate::tools::analysis::ARRANGEMENT
            | crate::tools::analysis::MIX
    )
}

/// Commands that never reach Live, and so cannot be part of a batch.
fn is_local(command: &str) -> bool {
    command.starts_with("__local_")
}

pub struct AbletonServer {
    live: Arc<LiveClient>,
    tools: Arc<ToolRegistry>,
}

impl AbletonServer {
    pub fn new(live: Arc<LiveClient>, tools: Arc<ToolRegistry>) -> Self {
        Self { live, tools }
    }

    /// Translate `{tool, arguments}` steps into the `{type, params}` pairs the
    /// Remote Script executes, so a batch validates against the same schemas as
    /// individual calls instead of being an unchecked passthrough to Live.
    fn expand_batch(&self, params: &mut Value) -> Result<(), String> {
        let Some(steps) = params.get("steps").and_then(Value::as_array).cloned() else {
            return Err("`steps` must be an array".to_string());
        };
        if steps.is_empty() {
            return Err("`steps` must contain at least one step".to_string());
        }

        let mut commands = Vec::with_capacity(steps.len());
        for (i, step) in steps.iter().enumerate() {
            let name = step
                .get("tool")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("step {i}: missing `tool`"))?;
            if name == "batch" {
                return Err(format!("step {i}: `batch` cannot contain another batch"));
            }
            let tool = self
                .tools
                .get(name)
                .ok_or_else(|| format!("step {i}: unknown tool `{name}`"))?;
            if is_local(tool.command) || is_analysis(tool.command) {
                return Err(format!(
                    "step {i}: `{name}` is answered by crableton rather than Live, so it \
                     cannot run inside a batch"
                ));
            }

            let args = match step.get("arguments") {
                Some(Value::Object(map)) => Some(map.clone()),
                Some(Value::Null) | None => None,
                Some(other) => {
                    return Err(format!("step {i}: `arguments` must be an object, got {other}"));
                }
            };
            let step_params = tool
                .build_params(args.as_ref())
                .map_err(|e| format!("step {i} (`{name}`): {e}"))?;

            commands.push(json!({ "type": tool.command, "params": step_params }));
        }

        let obj = params.as_object_mut().expect("params is an object");
        obj.remove("steps");
        obj.insert("commands".into(), Value::Array(commands));
        Ok(())
    }

    /// Gather one snapshot and compute the requested analysis from it.
    ///
    /// The snapshot is large, every clip, note and mixer value, but it stays
    /// here: only the analysis is returned, which is the whole point of doing
    /// this server-side rather than asking the model to reason over raw state.
    async fn run_analysis(&self, command: &str, params: &Value) -> CallToolResult {
        use crate::analysis::{self, Snapshot};
        use crate::tools::analysis as tools;

        let needs_notes = command != tools::ARRANGEMENT;
        let request = json!({ "include_notes": needs_notes, "include_params": false });

        let raw = match self.live.send("get_session_snapshot", request).await {
            Ok(raw) => raw,
            Err(e) => return tool_error(e),
        };
        let snapshot = Snapshot::from_value(&raw);

        let answer = match command {
            tools::HARMONY => analysis::analyze_harmony(
                &snapshot,
                params
                    .get("track_index")
                    .and_then(Value::as_u64)
                    .map(|i| i as usize),
                params
                    .get("max_bars")
                    .and_then(Value::as_u64)
                    .unwrap_or(64)
                    .clamp(1, 256) as usize,
            ),
            tools::ARRANGEMENT => analysis::arrangement::analyze(
                &snapshot,
                params
                    .get("min_section_bars")
                    .and_then(Value::as_u64)
                    .unwrap_or(2)
                    .clamp(1, 64) as usize,
            ),
            tools::MIX => analysis::mix::analyze(&snapshot),
            other => {
                return tool_error(Error::UnknownTool(other.to_string()));
            }
        };

        CallToolResult::success(vec![ContentBlock::text(render(&answer))])
    }

    async fn run_tool(&self, tool: &ToolDef, args: Option<&Map<String, Value>>) -> CallToolResult {
        let mut params = match tool.build_params(args) {
            Ok(params) => params,
            Err(reason) => {
                return tool_error(Error::BadArguments {
                    tool: tool.name.to_string(),
                    reason,
                });
            }
        };

        // Served from the bundled reference, it never touches the socket, so it
        // works with Live closed.
        if tool.command == LOCAL_LOM {
            let answer = crate::lom::lookup(
                params.get("class_name").and_then(Value::as_str),
                params.get("query").and_then(Value::as_str),
            );
            return CallToolResult::success(vec![ContentBlock::text(render(&answer))]);
        }

        if tool.command == LOCAL_DEVICE_REFERENCE {
            let answer = crate::devices::lookup(
                params.get("device").and_then(Value::as_str),
                params.get("query").and_then(Value::as_str),
            );
            return CallToolResult::success(vec![ContentBlock::text(render(&answer))]);
        }

        if is_analysis(tool.command) {
            return self.run_analysis(tool.command, &params).await;
        }

        if tool.name == "batch"
            && let Err(reason) = self.expand_batch(&mut params)
        {
            return tool_error(Error::BadArguments {
                tool: tool.name.to_string(),
                reason,
            });
        }

        // Fail with an actionable message rather than Live's bare
        // "Unknown command" when the installed script predates this tool.
        if !self.live.has_capability(tool.command).await {
            return tool_error(Error::Live(format!(
                "the Remote Script running in Live does not support `{}`. \
                 Run `crableton install` and restart Ableton Live to update it.",
                tool.command
            )));
        }

        match self.live.send(tool.command, params).await {
            Ok(result) => CallToolResult::success(vec![ContentBlock::text(render(&result))]),
            Err(e) => tool_error(e),
        }
    }
}

/// Tool-level failures are reported as results, not JSON-RPC errors: the model
/// should see "slot 3 is occupied" and adapt, not have the call vanish.
fn tool_error(e: Error) -> CallToolResult {
    let mut result = CallToolResult::success(vec![ContentBlock::text(format!("Error: {e}"))]);
    result.is_error = Some(true);
    result
}

/// Render a Live reply for the model: readable when small, compact when the
/// payload is a full session snapshot and indentation would be pure token cost.
fn render(result: &Value) -> String {
    match result {
        Value::Null => "OK".to_string(),
        Value::String(s) => s.clone(),
        Value::Object(map) if map.is_empty() => "OK".to_string(),
        _ => {
            let compact = serde_json::to_string(result).unwrap_or_else(|e| e.to_string());
            if compact.len() > 16 * 1024 {
                compact
            } else {
                serde_json::to_string_pretty(result).unwrap_or(compact)
            }
        }
    }
}

fn to_mcp_tool(def: &ToolDef) -> Tool {
    Tool::new(
        def.name,
        def.description.clone(),
        Arc::new(def.input_schema()),
    )
    .with_annotations(
        ToolAnnotations::new()
            .read_only(def.read_only)
            .destructive(def.destructive)
            // Every tool reaches out to a separate application we do not control.
            .open_world(true),
    )
}

impl ServerHandler for AbletonServer {
    fn get_info(&self) -> InitializeResult {
        InitializeResult::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(
                Implementation::new("crableton", env!("CARGO_PKG_VERSION"))
                    .with_title("Crableton, Ableton Live"),
            )
            .with_instructions(INSTRUCTIONS)
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(
            self.tools.all().iter().map(to_mcp_tool).collect(),
        ))
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.tools.get(name).map(to_mcp_tool)
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let Some(tool) = self.tools.get(request.name.as_ref()) else {
            // An unroutable tool name is a protocol-level mistake, so it is an
            // error rather than a result the model is meant to recover from.
            return Err(ErrorData::invalid_params(
                Cow::Owned(format!("unknown tool `{}`", request.name)),
                None,
            ));
        };
        Ok(self
            .run_tool(tool, request.arguments.as_ref())
            .await
            .into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;

    fn server() -> AbletonServer {
        AbletonServer::new(
            LiveClient::new(Config::default()),
            Arc::new(ToolRegistry::new(crate::tools::all())),
        )
    }

    #[test]
    fn batch_expands_steps_into_remote_commands() {
        let server = server();
        let mut params = json!({
            "steps": [
                { "tool": "set_tempo", "arguments": { "tempo": 128 } },
                { "tool": "create_midi_track", "arguments": {} }
            ]
        });
        server.expand_batch(&mut params).unwrap();

        let commands = params["commands"].as_array().unwrap();
        assert_eq!(commands.len(), 2);
        assert_eq!(commands[0]["type"], "set_tempo");
        assert_eq!(commands[0]["params"]["tempo"], 128.0);
        assert_eq!(commands[1]["type"], "create_midi_track");
        assert!(params.get("steps").is_none());
    }

    #[test]
    fn batch_rejects_bad_steps() {
        let server = server();
        for bad in [
            json!({ "steps": [] }),
            json!({ "steps": [{ "tool": "no_such_tool" }] }),
            json!({ "steps": [{ "tool": "batch", "arguments": { "steps": [] } }] }),
            json!({ "steps": [{ "tool": "set_tempo", "arguments": {} }] }),
        ] {
            let mut params = bad.clone();
            assert!(
                server.expand_batch(&mut params).is_err(),
                "should have rejected {bad}"
            );
        }
    }

    #[test]
    fn every_tool_has_a_usable_schema() {
        let registry = ToolRegistry::new(crate::tools::all());
        for tool in registry.all() {
            assert!(!tool.description.is_empty(), "{} has no description", tool.name);
            let schema = tool.input_schema();
            assert_eq!(schema["type"], "object", "{}", tool.name);
            for arg in &tool.args {
                assert!(
                    !arg.description.is_empty(),
                    "{}.{} has no description",
                    tool.name,
                    arg.name
                );
            }
        }
    }
}
