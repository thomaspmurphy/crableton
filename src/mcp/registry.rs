//! A declarative description of every tool we expose.
//!
//! Almost every tool is "validate arguments, forward them to a Remote Script
//! command, render the reply", so tools are data rather than functions: a
//! [`ToolDef`] carries its schema and the mapping from MCP arguments to Live
//! command parameters. Adding coverage means adding a row, not a handler.

use serde_json::{Map, Value, json};
use std::collections::HashMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArgTy {
    Int,
    Num,
    Str,
    Bool,
    /// Free-form JSON, used for the note and envelope payloads that carry their
    /// own documented shape.
    Any,
    /// Array of MIDI note objects.
    Notes,
    /// Array of `[time, value]` automation breakpoints.
    Points,
    /// Array of `{tool, arguments}` steps for the `batch` tool.
    Steps,
    /// Array of integers.
    IntList,
    /// Array of strings.
    StrList,
}

#[derive(Debug, Clone)]
pub struct ArgDef {
    pub name: &'static str,
    pub description: String,
    pub ty: ArgTy,
    pub required: bool,
    pub default: Option<Value>,
    pub choices: Option<Vec<&'static str>>,
}

impl ArgDef {
    fn schema(&self) -> Value {
        let mut schema = match self.ty {
            ArgTy::Int => json!({ "type": "integer" }),
            ArgTy::Num => json!({ "type": "number" }),
            ArgTy::Str => json!({ "type": "string" }),
            ArgTy::Bool => json!({ "type": "boolean" }),
            ArgTy::Any => json!({}),
            ArgTy::IntList => json!({ "type": "array", "items": { "type": "integer" } }),
            ArgTy::StrList => json!({ "type": "array", "items": { "type": "string" } }),
            ArgTy::Notes => json!({
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pitch", "start_time", "duration"],
                    "properties": {
                        "pitch": { "type": "integer", "minimum": 0, "maximum": 127,
                                   "description": "MIDI note number; 60 is middle C." },
                        "start_time": { "type": "number",
                                        "description": "Offset in beats from the clip start." },
                        "duration": { "type": "number", "description": "Length in beats." },
                        "velocity": { "type": "number", "minimum": 0, "maximum": 127,
                                      "description": "Defaults to 100." },
                        "mute": { "type": "boolean", "description": "Deactivate the note." },
                        "probability": { "type": "number", "minimum": 0, "maximum": 1,
                                         "description": "Live 11+ per-note chance." },
                        "velocity_deviation": { "type": "number",
                                                "description": "Live 11+ velocity randomisation range." },
                        "release_velocity": { "type": "number", "minimum": 0, "maximum": 127 }
                    }
                }
            }),
            ArgTy::Points => json!({
                "type": "array",
                "items": {
                    "type": "array",
                    "prefixItems": [{ "type": "number" }, { "type": "number" }],
                    "minItems": 2,
                    "maxItems": 2
                },
                "description": "Breakpoints as [time_in_beats, value] pairs, in ascending time order."
            }),
            ArgTy::Steps => json!({
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["tool"],
                    "properties": {
                        "tool": { "type": "string", "description": "Name of any other crableton tool." },
                        "arguments": { "type": "object", "description": "That tool's arguments." }
                    }
                }
            }),
        };

        let obj = schema.as_object_mut().expect("arg schema is an object");
        obj.insert("description".into(), json!(self.description));
        if let Some(choices) = &self.choices {
            obj.insert("enum".into(), json!(choices));
        }
        if let Some(default) = &self.default {
            obj.insert("default".into(), default.clone());
        }
        schema
    }
}

#[derive(Debug, Clone)]
pub struct ToolDef {
    pub name: &'static str,
    /// The Remote Script command this forwards to.
    pub command: &'static str,
    pub description: String,
    /// Which toolset this belongs to, for `CRABLETON_TOOLSETS` filtering.
    /// Assigned when the registry is assembled.
    pub group: &'static str,
    pub args: Vec<ArgDef>,
    /// Parameters always sent with this command, letting several tools share
    /// one Live-side handler (e.g. `set_track_volume` and `set_track_pan`).
    pub fixed: Vec<(&'static str, Value)>,
    pub read_only: bool,
    pub destructive: bool,
}

impl ToolDef {
    pub fn new(name: &'static str, command: &'static str, description: impl Into<String>) -> Self {
        Self {
            name,
            command,
            description: description.into(),
            group: "core",
            args: Vec::new(),
            fixed: Vec::new(),
            read_only: false,
            destructive: false,
        }
    }

    pub fn read_only(mut self) -> Self {
        self.read_only = true;
        self
    }

    /// Marks tools that can discard user work (deleting a track, replacing a
    /// clip's notes) so clients can prompt before running them.
    pub fn destructive(mut self) -> Self {
        self.destructive = true;
        self
    }

    pub fn fixed(mut self, key: &'static str, value: Value) -> Self {
        self.fixed.push((key, value));
        self
    }

    fn arg(mut self, name: &'static str, description: impl Into<String>, ty: ArgTy, required: bool) -> Self {
        self.args.push(ArgDef {
            name,
            description: description.into(),
            ty,
            required,
            default: None,
            choices: None,
        });
        self
    }

    fn with_last<F: FnOnce(&mut ArgDef)>(mut self, f: F) -> Self {
        f(self.args.last_mut().expect("no argument to modify"));
        self
    }

    pub fn int(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Int, true)
    }
    pub fn num(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Num, true)
    }
    pub fn text(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Str, true)
    }
    pub fn boolean(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Bool, true)
    }
    pub fn notes(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Notes, true)
    }
    pub fn points(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Points, true)
    }
    pub fn steps(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Steps, true)
    }

    pub fn opt_int(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Int, false)
    }
    pub fn opt_num(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Num, false)
    }
    pub fn opt_text(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Str, false)
    }
    pub fn opt_bool(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Bool, false)
    }
    pub fn opt_any(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Any, false)
    }
    pub fn opt_points(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::Points, false)
    }
    pub fn opt_int_list(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::IntList, false)
    }
    pub fn opt_str_list(self, name: &'static str, d: impl Into<String>) -> Self {
        self.arg(name, d, ArgTy::StrList, false)
    }

    /// Give the argument just added a default value (which also makes it optional).
    pub fn default(self, value: Value) -> Self {
        self.with_last(|a| {
            a.default = Some(value);
            a.required = false;
        })
    }

    /// Restrict the argument just added to a fixed set of strings.
    pub fn choices(self, choices: &[&'static str]) -> Self {
        self.with_last(|a| a.choices = Some(choices.to_vec()))
    }

    /// The tool's JSON Schema, as MCP wants it.
    pub fn input_schema(&self) -> Map<String, Value> {
        let mut properties = Map::new();
        let mut required = Vec::new();
        for arg in &self.args {
            properties.insert(arg.name.to_string(), arg.schema());
            if arg.required {
                required.push(json!(arg.name));
            }
        }

        let mut schema = Map::new();
        schema.insert("type".into(), json!("object"));
        schema.insert("properties".into(), Value::Object(properties));
        schema.insert("required".into(), Value::Array(required));
        schema.insert("additionalProperties".into(), json!(false));
        schema
    }

    /// Turn validated MCP arguments into the Remote Script's `params` object.
    ///
    /// Unknown keys are dropped rather than forwarded: a hallucinated argument
    /// should not reach Live as a silently ignored parameter.
    pub fn build_params(&self, args: Option<&Map<String, Value>>) -> Result<Value, String> {
        let empty = Map::new();
        let args = args.unwrap_or(&empty);

        let mut params = Map::new();
        for (key, value) in &self.fixed {
            params.insert((*key).to_string(), value.clone());
        }

        for arg in &self.args {
            match args.get(arg.name) {
                Some(Value::Null) | None => {
                    if let Some(default) = &arg.default {
                        params.insert(arg.name.to_string(), default.clone());
                    } else if arg.required {
                        return Err(format!("missing required argument `{}`", arg.name));
                    }
                }
                Some(value) => {
                    let value = coerce(arg, value)?;
                    params.insert(arg.name.to_string(), value);
                }
            }
        }

        if let Some(unknown) = args.keys().find(|k| {
            !self.args.iter().any(|a| a.name == k.as_str())
                && !self.fixed.iter().any(|(f, _)| f == k)
        }) {
            return Err(format!(
                "unknown argument `{unknown}`; expected one of: {}",
                self.args
                    .iter()
                    .map(|a| a.name)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }

        Ok(Value::Object(params))
    }
}

/// Accept the near-misses models routinely produce (`"3"` for an integer,
/// `4` for a float) while still rejecting genuinely wrong shapes.
fn coerce(arg: &ArgDef, value: &Value) -> Result<Value, String> {
    let name = arg.name;
    let coerced = match arg.ty {
        ArgTy::Int => match value {
            Value::Number(n) if n.is_i64() || n.is_u64() => value.clone(),
            Value::Number(n) => {
                let f = n.as_f64().unwrap_or_default();
                if f.fract() == 0.0 {
                    json!(f as i64)
                } else {
                    return Err(format!("`{name}` must be a whole number, got {f}"));
                }
            }
            Value::String(s) => s
                .trim()
                .parse::<i64>()
                .map(|v| json!(v))
                .map_err(|_| format!("`{name}` must be an integer, got {s:?}"))?,
            other => return Err(format!("`{name}` must be an integer, got {other}")),
        },
        ArgTy::Num => match value {
            Value::Number(_) => value.clone(),
            Value::String(s) => s
                .trim()
                .parse::<f64>()
                .map(|v| json!(v))
                .map_err(|_| format!("`{name}` must be a number, got {s:?}"))?,
            other => return Err(format!("`{name}` must be a number, got {other}")),
        },
        ArgTy::Bool => match value {
            Value::Bool(_) => value.clone(),
            Value::String(s) => match s.trim().to_ascii_lowercase().as_str() {
                "true" | "1" | "yes" | "on" => json!(true),
                "false" | "0" | "no" | "off" => json!(false),
                _ => return Err(format!("`{name}` must be a boolean, got {s:?}")),
            },
            other => return Err(format!("`{name}` must be a boolean, got {other}")),
        },
        ArgTy::Str => match value {
            Value::String(_) => value.clone(),
            other => return Err(format!("`{name}` must be a string, got {other}")),
        },
        ArgTy::Notes | ArgTy::Points | ArgTy::Steps | ArgTy::IntList | ArgTy::StrList => {
            match value {
                Value::Array(_) => value.clone(),
                // Models sometimes hand back the JSON they were asked to build.
                Value::String(s) => serde_json::from_str::<Value>(s)
                    .ok()
                    .filter(Value::is_array)
                    .ok_or_else(|| format!("`{name}` must be an array"))?,
                other => return Err(format!("`{name}` must be an array, got {other}")),
            }
        }
        ArgTy::Any => value.clone(),
    };

    if let Some(choices) = &arg.choices
        && let Some(got) = coerced.as_str()
        && !choices.contains(&got)
    {
        return Err(format!(
            "`{name}` must be one of {}, got {got:?}",
            choices.join(", ")
        ));
    }

    Ok(coerced)
}

/// All tools, indexed by name.
pub struct ToolRegistry {
    tools: Vec<ToolDef>,
    by_name: HashMap<&'static str, usize>,
}

impl ToolRegistry {
    pub fn new(tools: Vec<ToolDef>) -> Self {
        let mut by_name = HashMap::with_capacity(tools.len());
        for (index, tool) in tools.iter().enumerate() {
            if by_name.insert(tool.name, index).is_some() {
                panic!("duplicate tool name `{}`", tool.name);
            }
        }
        Self { tools, by_name }
    }

    pub fn get(&self, name: &str) -> Option<&ToolDef> {
        self.by_name.get(name).map(|&i| &self.tools[i])
    }

    pub fn all(&self) -> &[ToolDef] {
        &self.tools
    }

    pub fn len(&self) -> usize {
        self.tools.len()
    }

    pub fn is_empty(&self) -> bool {
        self.tools.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ToolDef {
        ToolDef::new("set_track_volume", "set_track_mixer", "Set a track's volume.")
            .int("track_index", "Track index.")
            .num("volume", "0.0 to 1.0.")
            .opt_text("track_type", "Which track list.")
            .choices(&["regular", "return", "master"])
            .default(json!("regular"))
            .fixed("field", json!("volume"))
    }

    #[test]
    fn builds_params_with_fixed_and_defaults() {
        let args: Map<String, Value> =
            serde_json::from_value(json!({ "track_index": 2, "volume": 0.7 })).unwrap();
        let params = sample().build_params(Some(&args)).unwrap();
        assert_eq!(
            params,
            json!({ "field": "volume", "track_index": 2, "volume": 0.7, "track_type": "regular" })
        );
    }

    #[test]
    fn coerces_stringly_typed_numbers() {
        let args: Map<String, Value> =
            serde_json::from_value(json!({ "track_index": "2", "volume": "0.7" })).unwrap();
        let params = sample().build_params(Some(&args)).unwrap();
        assert_eq!(params["track_index"], json!(2));
        assert_eq!(params["volume"], json!(0.7));
    }

    #[test]
    fn rejects_missing_required_unknown_and_out_of_enum() {
        let missing: Map<String, Value> =
            serde_json::from_value(json!({ "track_index": 0 })).unwrap();
        assert!(sample().build_params(Some(&missing)).unwrap_err().contains("volume"));

        let unknown: Map<String, Value> =
            serde_json::from_value(json!({ "track_index": 0, "volume": 0.5, "nope": 1 })).unwrap();
        assert!(sample().build_params(Some(&unknown)).unwrap_err().contains("nope"));

        let bad_enum: Map<String, Value> =
            serde_json::from_value(json!({ "track_index": 0, "volume": 0.5, "track_type": "bus" }))
                .unwrap();
        assert!(sample().build_params(Some(&bad_enum)).unwrap_err().contains("bus"));
    }

    #[test]
    fn schema_marks_only_required_args() {
        let schema = sample().input_schema();
        assert_eq!(schema["required"], json!(["track_index", "volume"]));
        assert_eq!(schema["properties"]["track_type"]["default"], json!("regular"));
    }
}
