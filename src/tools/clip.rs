use serde_json::json;

use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

const QUANTIZE_GRID: &[&str] = &[
    "1/4", "1/4T", "1/8", "1/8T", "1/16", "1/16T", "1/32", "1/32T",
];

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_clip",
            "get_clip",
            "Everything about one clip: name, colour, length, loop and marker positions, launch \
             settings, follow actions, and — for audio clips — warp mode, gain and pitch.",
        )
        .read_only()
        .clip_ref(),

        ToolDef::new(
            "create_clip",
            "create_clip",
            "Create an empty MIDI clip in a Session clip slot. The slot must be empty — delete \
             what is there first if you mean to replace it.",
        )
        .track_ref()
        .int("clip_index", "0-based clip slot index.")
        .opt_num("length", "Clip length in beats. 4.0 is one bar in 4/4.")
        .default(json!(4.0)),

        ToolDef::new(
            "create_audio_clip",
            "create_audio_clip",
            "Import an audio file into a Session clip slot on an audio track. Requires Live \
             12.0.5 or newer.",
        )
        .track_ref()
        .int("clip_index", "0-based clip slot index. The slot must be empty.")
        .text("path", "Absolute path to an audio file Live can read, such as a .wav or .aif."),

        ToolDef::new("delete_clip", "delete_clip", "Delete a clip, freeing its slot.")
            .destructive()
            .clip_ref(),

        ToolDef::new(
            "duplicate_clip",
            "duplicate_clip",
            "Copy a Session clip into another slot, on the same track or a different one.",
        )
        .clip_ref()
        .opt_int("target_track_index", "Destination track. Defaults to the source track.")
        .int("target_clip_index", "Destination clip slot, which must be empty."),

        ToolDef::new("fire_clip", "fire_clip", "Launch a clip.").clip_ref(),

        ToolDef::new("stop_clip", "stop_clip", "Stop a playing clip.").clip_ref(),

        ToolDef::new(
            "set_clip_properties",
            "set_clip_properties",
            "Change any combination of a clip's properties in one call — name and colour, loop \
             and markers, launch behaviour, follow actions, and audio warping. Omit anything \
             you do not want to touch. Audio-only fields are rejected on MIDI clips.",
        )
        .clip_ref()
        .opt_text("name", "Clip name.")
        .opt_int("color", "Colour as 0xRRGGBB.")
        .opt_bool("muted", "Deactivate the clip without deleting it.")
        .opt_bool("looping", "Whether the clip loops.")
        .opt_num("loop_start", "Loop start in beats from the clip's origin.")
        .opt_num("loop_end", "Loop end in beats. Must be greater than `loop_start`.")
        .opt_num("start_marker", "Playback start marker in beats.")
        .opt_num("end_marker", "Playback end marker in beats.")
        .opt_int("signature_numerator", "Clip time signature: beats per bar.")
        .opt_int("signature_denominator", "Clip time signature: beat unit.")
        .opt_text("launch_mode", "How the clip responds to being launched.")
        .choices(&["trigger", "gate", "toggle", "repeat"])
        .opt_text("launch_quantization", "Launch quantization for this clip. \"global\" follows the set.")
        .choices(&[
            "global", "none", "8 bars", "4 bars", "2 bars", "1 bar", "1/2", "1/2T", "1/4",
            "1/4T", "1/8", "1/8T", "1/16", "1/16T", "1/32",
        ])
        .opt_bool("legato", "Launch in legato mode, continuing from the outgoing clip's position.")
        .opt_num("velocity_amount", "How much launch velocity affects the clip, 0.0 to 1.0.")
        .opt_bool("follow_action_enabled", "Whether the clip triggers a follow action.")
        .opt_num("follow_action_time", "Beats before the follow action fires.")
        .opt_text("follow_action_a", "First follow action.")
        .choices(&["none", "stop", "play again", "previous", "next", "first", "last", "any", "other", "jump"])
        .opt_text("follow_action_b", "Second follow action.")
        .choices(&["none", "stop", "play again", "previous", "next", "first", "last", "any", "other", "jump"])
        .opt_int("follow_action_chance_a", "Relative weight of the first follow action.")
        .opt_int("follow_action_chance_b", "Relative weight of the second follow action.")
        .opt_bool("warping", "Audio clips only: whether the clip is warped to the set's tempo.")
        .opt_text("warp_mode", "Audio clips only: the warping algorithm.")
        .choices(&["beats", "tones", "texture", "repitch", "complex", "complex pro"])
        .opt_num("gain", "Audio clips only: clip gain from 0.0 to 1.0, where 0.4 is unity.")
        .opt_int("pitch_coarse", "Audio clips only: transposition in semitones, -48 to 48.")
        .opt_num("pitch_fine", "Audio clips only: fine tuning in cents, -50 to 50.")
        .opt_bool("ram_mode", "Audio clips only: load the sample into RAM."),

        ToolDef::new(
            "quantize_clip",
            "quantize_clip",
            "Snap a clip's contents to a grid. On MIDI clips this moves notes; on warped audio \
             clips it moves warp markers.",
        )
        .clip_ref()
        .opt_text("grid", "Grid to quantize to.")
        .choices(QUANTIZE_GRID)
        .default(json!("1/16"))
        .opt_num(
            "amount",
            "How far towards the grid to move, 0.0 to 1.0. Use less than 1.0 to tighten timing \
             while keeping some of the original feel.",
        )
        .default(json!(1.0)),

        ToolDef::new(
            "crop_clip",
            "crop_clip",
            "Discard everything outside the clip's loop brace, permanently.",
        )
        .destructive()
        .clip_ref(),

        ToolDef::new(
            "duplicate_clip_loop",
            "duplicate_clip_loop",
            "Double the clip's loop length, copying the existing contents into the new half. \
             The usual way to turn a 1-bar idea into a 2-bar one before varying it.",
        )
        .clip_ref(),

        ToolDef::new(
            "get_clip_envelope",
            "get_clip_envelope",
            "Read a clip's automation for one parameter as breakpoints. Returns nothing if the \
             clip has no envelope for that parameter.",
        )
        .read_only()
        .clip_ref()
        .int(
            "device_index",
            "Device that owns the parameter, or -1 for the track's own mixer.",
        )
        .int(
            "parameter_index",
            "Index into that device's parameters, as reported by `get_device_parameters`. For \
             the mixer (device_index -1): 0 is volume, 1 is panning, 2 is the track on/off \
             switch, and 3 onwards are the sends in order.",
        ),

        ToolDef::new(
            "set_clip_envelope",
            "set_clip_envelope",
            "Write clip automation for one parameter, replacing any existing envelope. This is \
             how you draw a filter sweep, a volume fade or any other moving value — including \
             track volume and pan, via `device_index: -1`.\n\n\
             Give the shape as breakpoints and it is filled in for you. Live's automation API \
             only writes stepped values, so a smooth ramp is many small steps: with \
             `interpolation: \"linear\"` the steps between your breakpoints are generated at \
             `resolution` beats apart. Coarser resolution means fewer steps and a faster write; \
             1/16 of a beat is smooth enough for filter sweeps and fades.\n\n\
             Automation is per clip and its times are relative to the clip's own start, not \
             the Arrangement.\n\n\
             Live only creates clip automation on **Session** clips — it refuses on \
             Arrangement clips. So the order matters: write the envelope on the Session clip \
             first, then `duplicate_clip_to_arrangement`, and every copy carries it. An \
             Arrangement clip that is already placed cannot be automated through the API.",
        )
        .clip_ref()
        .int("device_index", "Device that owns the parameter, or -1 for the track's own mixer.")
        .int("parameter_index", "Index into that device's parameters. See `get_clip_envelope` for the mixer mapping.")
        .points(
            "points",
            "Breakpoints as [time_in_beats, value] pairs in ascending time order, with times \
             relative to the clip start. Values are in the parameter's own units — check them \
             with `get_device_parameters` — and are clamped to its range. A single breakpoint \
             holds one constant value across the clip.",
        )
        .opt_text(
            "interpolation",
            "\"linear\" ramps between breakpoints; \"step\" holds each value until the next.",
        )
        .choices(&["linear", "step"])
        .default(json!("linear"))
        .opt_num(
            "resolution",
            "Spacing in beats of the generated steps when interpolating. Smaller is smoother \
             and slower to write.",
        )
        .default(json!(0.0625)),

        ToolDef::new(
            "clear_clip_envelope",
            "clear_clip_envelope",
            "Remove a clip's automation for one parameter, returning it to a static value.",
        )
        .destructive()
        .clip_ref()
        .int("device_index", "Device that owns the parameter, or -1 for the track's own mixer.")
        .int("parameter_index", "Index into that device's parameters."),
    ]
}
