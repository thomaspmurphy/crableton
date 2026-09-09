use serde_json::json;

use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_tracks",
            "get_tracks",
            "List every track (regular, return and master) with its index, name, colour, \
             type, mixer state and arm/mute/solo flags.",
        )
        .read_only()
        .opt_bool("include_devices", "Include each track's device chain.")
        .default(json!(true))
        .opt_bool("include_clips", "Include a summary of each track's Session clips.")
        .default(json!(false)),

        ToolDef::new(
            "get_track_info",
            "get_track_info",
            "Everything about one track: mixer values, routing, devices, Session clip slots and \
             Arrangement clips.",
        )
        .read_only()
        .track_ref(),

        ToolDef::new(
            "create_midi_track",
            "create_midi_track",
            "Add a MIDI track, for instruments and MIDI clips.",
        )
        .opt_int("index", "Where to insert it. -1 appends to the end.")
        .default(json!(-1)),

        ToolDef::new(
            "create_audio_track",
            "create_audio_track",
            "Add an audio track, for samples, stems and recorded audio.",
        )
        .opt_int("index", "Where to insert it. -1 appends to the end.")
        .default(json!(-1)),

        ToolDef::new(
            "create_return_track",
            "create_return_track",
            "Add a return track, for send effects like reverb and delay shared across tracks.",
        ),

        ToolDef::new(
            "delete_track",
            "delete_track",
            "Delete a track and everything on it. The master track cannot be deleted.",
        )
        .destructive()
        .track_ref(),

        ToolDef::new(
            "duplicate_track",
            "duplicate_track",
            "Duplicate a track with its devices and clips, inserting the copy to its right.",
        )
        .track_ref(),

        ToolDef::new("set_track_name", "set_track_name", "Rename a track.")
            .track_ref()
            .text("name", "New track name."),

        ToolDef::new(
            "set_track_mixer",
            "set_track_mixer",
            "Change any combination of a track's mixer and state controls in one call. Omit \
             anything you do not want to touch.",
        )
        .track_ref()
        .opt_num(
            "volume",
            "Fader position from 0.0 to 1.0, not decibels. 0.85 is unity (0 dB), 1.0 is +6 dB.",
        )
        .opt_num("panning", "-1.0 hard left, 0.0 centre, 1.0 hard right.")
        .opt_bool("mute", "Mute the track.")
        .opt_bool("solo", "Solo the track.")
        .opt_bool("arm", "Arm the track for recording. Return and master tracks cannot be armed.")
        .opt_int("color", "Colour as 0xRRGGBB, e.g. 65280 for green.")
        .opt_bool("folded", "For a group track, whether it is collapsed.")
        .opt_text("crossfade_assign", "Crossfader assignment.")
        .choices(&["a", "none", "b"])
        .opt_text("monitoring_state", "Input monitoring mode.")
        .choices(&["in", "auto", "off"]),

        ToolDef::new(
            "set_track_send",
            "set_track_send",
            "Set how much of a track is sent to a return track.",
        )
        .track_ref()
        .int("send_index", "0-based index of the return track: 0 is Send A, 1 is Send B.")
        .num("value", "Send amount from 0.0 (off) to 1.0."),

        ToolDef::new(
            "get_track_routing",
            "get_track_routing",
            "Read a track's input and output routing, together with every routing type and \
             channel Live will accept for it. Call this before `set_track_routing`. The valid \
             names depend on the set's tracks and the machine's audio and MIDI hardware.",
        )
        .read_only()
        .track_ref(),

        ToolDef::new(
            "set_track_routing",
            "set_track_routing",
            "Point a track's input or output somewhere else: feeding a track into another \
             track, picking a hardware input, or choosing a MIDI channel. Names must match \
             those reported by `get_track_routing` exactly.",
        )
        .track_ref()
        .opt_text("input_type", "Input source, e.g. \"Ext. In\", \"Resampling\", or a track name.")
        .opt_text("input_channel", "Channel within the input source, e.g. \"1/2\" or \"Ch. 1\".")
        .opt_text("output_type", "Output destination, e.g. \"Master\", \"Ext. Out\", or a track name.")
        .opt_text("output_channel", "Channel within the output destination."),
    ]
}
