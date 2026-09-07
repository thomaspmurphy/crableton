use serde_json::json;

use crate::mcp::ToolDef;

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_remote_script_info",
            "get_script_info",
            "Report the version and command list of the Remote Script currently loaded in \
             Live. Use this when a tool reports that a command is unsupported.",
        )
        .read_only(),

        ToolDef::new(
            "batch",
            "batch",
            "Run several tools as one operation. Every step executes in a single pass on \
             Live's main thread, so a sequence that would otherwise cost one round trip and \
             one audio-thread tick each is applied at once — far faster, and the user sees a \
             single change rather than a flicker of intermediate states. Prefer this whenever \
             you are making more than two edits. Steps run in order and each result is \
             returned positionally; a step that fails stops the batch unless \
             `stop_on_error` is false.",
        )
        .steps(
            "steps",
            "Ordered steps, each `{\"tool\": \"<tool name>\", \"arguments\": {...}}`. \
             Arguments are validated against that tool's own schema.",
        )
        .opt_bool(
            "stop_on_error",
            "Abort the remaining steps when one fails. Set false to apply everything that can \
             be applied and collect the errors.",
        )
        .default(json!(true)),

        ToolDef::new(
            "lom_reference",
            "__local_lom",
            "Look up Ableton's Live Object Model — the API surface the Remote Script drives: \
             which properties and methods exist on Song, Track, Clip, Device, DeviceParameter, \
             Scene and friends, their types, and whether they are writable. Use this to \
             understand what is and is not reachable in Live before assuming a tool is missing, \
             and to interpret device and clip fields returned by the other tools. Call with no \
             arguments for the class index.",
        )
        .read_only()
        .opt_text(
            "class_name",
            "A Live Object Model class to describe in full, e.g. \"Clip\", \"DeviceParameter\", \
             \"Song.View\". Case-insensitive.",
        )
        .opt_text(
            "query",
            "Free-text search across class, property and method names and their descriptions, \
             e.g. \"warp\", \"quantize\", \"follow action\".",
        ),

        ToolDef::new(
            "describe_live_object",
            "describe_live_object",
            "Report what a Live object actually offers in *this* Live version: its class, its \
             readable properties with current values, and its methods. Where `lom_reference` \
             is a bundled description of the API in general, this interrogates the running \
             instance — use it when a property is missing, a value is not what you expected, \
             or the docs and reality disagree. The `envelopes` target lists every automation \
             envelope a clip carries and what each one automates, which is the only way to \
             see the automation on a placed Arrangement clip.",
        )
        .read_only()
        .opt_text("target", "Which object to describe.")
        .choices(&[
            "song", "song_view", "application", "track", "mixer", "clip_slot", "clip",
            "device", "parameter", "scene", "envelope", "envelopes",
        ])
        .default(json!("song"))
        .opt_text("filter", "Only report members whose name contains this text, e.g. \"envelope\".")
        .opt_int("track_index", "Track, for targets that need one.")
        .opt_text("track_type", "Which track list `track_index` indexes into.")
        .choices(&["regular", "return", "master"])
        .default(json!("regular"))
        .opt_int("clip_index", "Clip slot or arrangement position, for clip targets.")
        .opt_text("view", "Which view the clip lives in.")
        .choices(&["session", "arrangement"])
        .default(json!("session"))
        .opt_int("device_index", "Device, for device and parameter targets. -1 is the track mixer.")
        .opt_text("chain_path", "Dot-separated chain/device indices to reach a device inside a rack.")
        .opt_int("parameter_index", "Parameter, for parameter and envelope targets.")
        .opt_text("parameter_name", "Parameter by name instead of index.")
        .opt_int("scene_index", "Scene, for the scene target."),

        ToolDef::new(
            "device_reference",
            "__local_device_reference",
            "What Live's stock devices actually do: which of a device's parameters matter,              what each one changes musically, which are worth automating, and the traps              (Roar's saturation crowding the parts above it, Auto Filter's Filter Morph being              inert outside Morph mode, reverb on a bass muddying a mix). Pair it with              `get_device_parameters`, which gives the live values but cannot say which of              forty knobs is the one you want. Call with no arguments to list the devices              covered.",
        )
        .read_only()
        .opt_text(
            "device",
            "A device to describe, by the `class_name` Live reports (e.g. \"InstrumentVector\",              \"AutoFilter\") or its display name (\"Wavetable\", \"Auto Filter\").",
        )
        .opt_text(
            "query",
            "Free-text search over device summaries and parameters, e.g. \"cutoff\",              \"saturation\", \"widen\".",
        ),
    ]
}
