use serde_json::json;

use crate::mcp::ToolDef;

/// Live's launch-quantization values, spelled the way a musician would.
const GRID: &[&str] = &[
    "none", "8 bars", "4 bars", "2 bars", "1 bar", "1/2", "1/2T", "1/4", "1/4T", "1/8", "1/8T",
    "1/16", "1/16T", "1/32",
];

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_session_info",
            "get_session_info",
            "Overview of the open Live set: tempo, time signature, transport and record state, \
             loop region, track and scene counts, and the names of the tracks and scenes. \
             Start here: it is the cheapest way to orient yourself before making changes.",
        )
        .read_only(),

        ToolDef::new(
            "get_session_snapshot",
            "get_session_snapshot",
            "Full state of the set: every track with its mixer, devices, session clips and \
             arrangement clips, plus return and master tracks, scenes and locators. This is a \
             large payload. Prefer `get_session_info` and the targeted `get_*` tools unless \
             you genuinely need everything at once.",
        )
        .read_only()
        .opt_bool("include_notes", "Include the MIDI notes of every clip.")
        .default(json!(false))
        .opt_bool("include_params", "Include every device's parameter values.")
        .default(json!(false)),

        ToolDef::new("set_tempo", "set_tempo", "Set the tempo of the Live set.")
            .num("tempo", "Beats per minute, 20.0 to 999.0."),

        ToolDef::new(
            "tap_tempo",
            "tap_tempo",
            "Tap Live's tempo control once. Repeated taps set the tempo from their spacing.",
        ),

        ToolDef::new(
            "start_playback",
            "start_playback",
            "Start the transport from the current playhead position.",
        ),

        ToolDef::new("stop_playback", "stop_playback", "Stop the transport."),

        ToolDef::new(
            "continue_playback",
            "continue_playback",
            "Resume the transport from where it was last stopped, rather than from the start \
             marker.",
        ),

        ToolDef::new(
            "stop_all_clips",
            "stop_all_clips",
            "Stop every playing Session clip, as the Stop All Clips button does. The transport \
             keeps running.",
        ),

        ToolDef::new(
            "set_song_time",
            "set_current_song_time",
            "Move the Arrangement playhead.",
        )
        .num("time", "Position in beats from the start of the Arrangement; 0.0 is the start."),

        ToolDef::new(
            "set_loop_region",
            "set_loop_region",
            "Set the Arrangement loop brace. Beats, not bars: in 4/4, bar 5 begins at beat 16.",
        )
        .opt_num("start", "Loop start, in beats from the start of the Arrangement.")
        .opt_num("length", "Loop length in beats.")
        .opt_bool("enabled", "Whether the loop is active."),

        ToolDef::new(
            "set_time_signature",
            "set_time_signature",
            "Set the time signature of the Live set.",
        )
        .int("numerator", "Beats per bar, 1 to 99. The 4 in 4/4.")
        .int("denominator", "Note value that gets the beat: 1, 2, 4, 8, 16, 32 or 64. The lower 4 in 4/4."),

        ToolDef::new(
            "set_transport_options",
            "set_transport_options",
            "Change any combination of the transport and recording switches in one call. Omit \
             anything you do not want to touch.",
        )
        .opt_bool("metronome", "Click on or off.")
        .opt_bool("record_mode", "Arrangement record arm (the main Record button).")
        .opt_bool("session_record", "Session record arm.")
        .opt_bool("arrangement_overdub", "Overdub into existing Arrangement clips instead of replacing.")
        .opt_bool("session_automation_record", "Capture automation while recording in Session view.")
        .opt_bool("punch_in", "Honour the loop start as a punch-in point.")
        .opt_bool("punch_out", "Honour the loop end as a punch-out point.")
        .opt_bool("follow_song", "Scroll the view to follow the playhead.")
        .opt_bool("back_to_arranger", "Clear the 'Back to Arrangement' state, re-enabling Arrangement playback after Session clips overrode it.")
        .opt_text("clip_trigger_quantization", "Global launch quantization for clips and scenes.")
        .choices(GRID)
        .opt_text("midi_recording_quantization", "Quantization applied to newly recorded MIDI.")
        .choices(&["none", "1/4", "1/8", "1/8T", "1/8 + 1/8T", "1/16", "1/16T", "1/16 + 1/16T", "1/32"])
        .opt_num("groove_amount", "Global groove intensity, 0.0 to 1.0."),

        ToolDef::new(
            "undo",
            "undo",
            "Undo the last change in Live, including changes the user made by hand. Live's \
             undo history is shared, so this is not limited to your own edits.",
        )
        .destructive(),

        ToolDef::new("redo", "redo", "Redo the last undone change in Live."),

        ToolDef::new(
            "capture_midi",
            "capture_midi",
            "Capture recently played MIDI into a clip, as Live's Capture button does.",
        ),

        ToolDef::new(
            "set_scale",
            "set_scale",
            "Set the set's root note and scale (Live 12). This drives Live's scale-aware \
             editing and the pitch names shown in clips.",
        )
        .opt_int("root_note", "0 to 11, where 0 is C and 11 is B.")
        .opt_text(
            "scale_name",
            "Scale name exactly as Live spells it, e.g. \"Major\", \"Minor\", \"Dorian\", \
             \"Mixolydian\", \"Harmonic Minor\", \"Pentatonic\".",
        )
        .opt_bool("scale_mode", "Whether scale highlighting is active."),

        ToolDef::new("set_view", "set_view", "Bring a part of Live's interface to the front.")
            .text("view", "Which view to show.")
            .choices(&["session", "arrangement", "detail_clip", "detail_device", "browser"]),

        ToolDef::new(
            "set_selection",
            "set_selection",
            "Move Live's selection, so the user's screen follows what you are describing. \
             Purely a view change, it edits nothing.",
        )
        .opt_int("track_index", "Track to select.")
        .opt_int("scene_index", "Scene to select.")
        .opt_int("clip_index", "Clip slot to select on the selected track.")
        .opt_int("device_index", "Device to select on the selected track."),
    ]
}
