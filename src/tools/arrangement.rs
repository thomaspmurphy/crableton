use serde_json::json;

use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_arrangement_clips",
            "get_arrangement_clips",
            "List the clips placed in a track's Arrangement timeline, in time order, with each \
             clip's name, start and end. The positions returned here are the `clip_index` \
             values the other Arrangement tools expect.",
        )
        .read_only()
        .track_ref()
        .opt_int(
            "take_lane_index",
            "List one take lane's clips instead of the main lane's, as numbered by `get_take_lanes`.",
        ),

        ToolDef::new(
            "create_arrangement_clip",
            "create_arrangement_clip",
            "Create an empty MIDI clip directly in a MIDI track's Arrangement timeline, then fill \
             it with the note tools using the returned `clip_index` and `view: \"arrangement\"`. \
             The span must be free: delete what is there first if you mean to replace it. Needs \
             a recent Live 12 release; on older versions build the clip in the Session and use \
             `duplicate_clip_to_arrangement`.",
        )
        .track_ref()
        .num("start_time", "Where the clip starts, in beats from the start of the Arrangement. In 4/4, bar 17 is beat 64.")
        .opt_num("length", "Clip length in beats. 4.0 is one bar in 4/4.")
        .default(json!(4.0))
        .opt_int(
            "take_lane_index",
            "Create the clip on this take lane, as numbered by `get_take_lanes`, instead of the main lane.",
        ),

        ToolDef::new(
            "create_arrangement_audio_clip",
            "create_arrangement_audio_clip",
            "Import an audio file onto an audio track's Arrangement timeline at a position. The \
             clip takes the file's length, so check what follows it with \
             `get_arrangement_clips`; only the start position is checked for an existing clip. \
             Needs a recent Live 12 release.",
        )
        .track_ref()
        .text("path", "Absolute path to an audio file Live can read, such as a .wav or .aif.")
        .num("start_time", "Where the clip starts, in beats from the start of the Arrangement.")
        .opt_int(
            "take_lane_index",
            "Place the clip on this take lane, as numbered by `get_take_lanes`, instead of the main lane.",
        ),

        ToolDef::new(
            "duplicate_clip_to_arrangement",
            "duplicate_clip_to_arrangement",
            "Copy a Session clip into the Arrangement timeline on the same track. Use `repeats` \
             to lay down a run of consecutive copies: a 4-bar loop across 32 bars is one call, \
             not eight. A span already occupied by another clip is refused. The copies are \
             returned with their Arrangement `clip_index`, and `envelopes` lists each parameter \
             the Session clip automates with whether the copies carry that envelope. Each is \
             `carried` with `via` `clip` (envelope on the copy) or `track_lane` (track \
             automation on the Arrangement lane, which the API cannot read or edit; rewrite \
             the Session clip envelope and copy again). Envelopes on devices nested in racks \
             are not detected.",
        )
        .track_ref()
        .int("clip_index", "Session clip slot to copy from.")
        .num("destination_time", "Where the first copy starts, in beats from the start of the Arrangement.")
        .opt_int(
            "repeats",
            "How many consecutive copies to place, each following the previous one.",
        )
        .default(json!(1)),

        ToolDef::new(
            "delete_arrangement_clip",
            "delete_arrangement_clip",
            "Delete one clip from a track's Arrangement timeline.",
        )
        .destructive()
        .track_ref()
        .int("clip_index", "Position in the track's Arrangement, as listed by `get_arrangement_clips`.")
        .opt_int(
            "take_lane_index",
            "Delete from this take lane instead of the main lane. Live 12.4's API may refuse \
             this; the error says so.",
        ),

        ToolDef::new(
            "clear_arrangement",
            "clear_arrangement",
            "Delete Arrangement clips in bulk: a whole track, a beat range, or the entire \
             Arrangement across every track. This is how you undo a previous arrangement pass \
             before laying down a new one.",
        )
        .destructive()
        .opt_int(
            "track_index",
            "Restrict to one track. Omit to clear the Arrangement on every track.",
        )
        .opt_text("expect_track_name", crate::tools::EXPECT_TRACK_NAME)
        .opt_num("from_time", "Only delete clips starting at or after this beat.")
        .opt_num("to_time", "Only delete clips starting before this beat."),

        ToolDef::new(
            "get_take_lanes",
            "get_take_lanes",
            "List a track's take lanes (Live 12 comping) with each lane's name and clips. A lane's \
             index is the `take_lane_index` the clip and Arrangement tools accept, and each \
             clip's index within its lane is its `clip_index`.",
        )
        .read_only()
        .track_ref(),

        ToolDef::new(
            "create_take_lane",
            "create_take_lane",
            "Add a take lane to a track, optionally named, for comping or for laying out \
             alternative versions of a part. Fill it with `create_arrangement_clip` and its \
             `take_lane_index`.",
        )
        .track_ref()
        .opt_text("name", "Lane name, e.g. \"Take 3\" or \"Alt chorus\"."),

        ToolDef::new(
            "start_arrangement_recording",
            "start_arrangement_recording",
            "Record into the Arrangement: arm the given tracks, optionally move the playhead, \
             press Record and start the transport. Without `overdub`, armed tracks replace what \
             is already in the recorded span. Stop with `stop_arrangement_recording`. For the \
             Record button alone use `set_transport_options`.",
        )
        .destructive()
        .opt_int_list(
            "arm_tracks",
            "Regular track indices to record-arm first. Tracks already armed stay armed.",
        )
        .opt_num("from_time", "Beat to start recording from. Omit to record from the current playhead position.")
        .opt_bool("overdub", "Set Arrangement overdub: add to existing clips rather than replacing them."),

        ToolDef::new(
            "stop_arrangement_recording",
            "stop_arrangement_recording",
            "Stop recording into the Arrangement and, by default, stop the transport. Reports \
             the tracks that were armed and their Arrangement clip counts, read once Live has \
             stopped, so the new takes can be found with `get_arrangement_clips`.",
        )
        .opt_bool("stop_playback", "Also stop the transport. False keeps playing with Record off.")
        .default(json!(true))
        .opt_bool("disarm", "Disarm every armed track afterwards.")
        .default(json!(false)),

        ToolDef::new(
            "get_locators",
            "get_locators",
            "List the Arrangement locators (cue points) with their names and beat positions. \
             The index of each is what `jump_to_locator` and `delete_locator` expect.",
        )
        .read_only(),

        ToolDef::new(
            "create_locator",
            "create_locator",
            "Place a named locator in the Arrangement, or rename the one already at that \
             position. Useful for marking out sections before arranging. Live can only toggle \
             a locator at the playhead, so the playhead is moved there first (a playing \
             transport pauses briefly). If Live will not put the playhead on that exact beat \
             (a stopped playhead can snap to the Arrangement grid), nothing is created and \
             the error names the beat it reached. Not available in `batch`.",
        )
        .text("name", "Locator label, e.g. \"Chorus\" or \"Drop\".")
        .num("time", "Position in beats from the start of the Arrangement. In 4/4, bar 17 is beat 64."),

        ToolDef::new(
            "delete_locator",
            "delete_locator",
            "Delete one Arrangement locator. The playhead jumps to it and the toggle only \
             happens once the playhead is on it, so a miss deletes nothing. Not available in \
             `batch`.",
        )
            .destructive()
            .int("index", "0-based index, as listed by `get_locators`."),

        ToolDef::new(
            "clear_locators",
            "clear_locators",
            "Delete every Arrangement locator, or all of those within a beat range, one at a \
             time with the same checks as `delete_locator`. Not available in `batch`.",
        )
        .destructive()
        .opt_num("from_time", "Only delete locators at or after this beat.")
        .opt_num("to_time", "Only delete locators before this beat."),

        ToolDef::new(
            "jump_to_locator",
            "jump_to_locator",
            "Move the playhead to a locator.",
        )
        .int("index", "0-based index, as listed by `get_locators`."),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, Value};

    fn tool(name: &str) -> ToolDef {
        tools().into_iter().find(|t| t.name == name).expect("tool exists")
    }

    fn args(value: Value) -> Map<String, Value> {
        serde_json::from_value(value).unwrap()
    }

    #[test]
    fn create_arrangement_clip_defaults_to_one_bar_on_the_main_lane() {
        let def = tool("create_arrangement_clip");
        let params = def
            .build_params(Some(&args(json!({ "track_index": 1, "start_time": 64 }))))
            .unwrap();
        assert_eq!(params["length"], json!(4.0));
        assert!(params.get("take_lane_index").is_none());
        assert_eq!(def.input_schema()["required"], json!(["track_index", "start_time"]));
    }

    #[test]
    fn audio_clips_need_a_path() {
        let err = tool("create_arrangement_audio_clip")
            .build_params(Some(&args(json!({ "track_index": 0, "start_time": 0 }))))
            .unwrap_err();
        assert!(err.contains("path"));
    }

    #[test]
    fn recording_over_clips_is_destructive_and_stopping_is_not() {
        assert!(tool("start_arrangement_recording").destructive);
        let params = tool("start_arrangement_recording")
            .build_params(Some(&args(json!({ "arm_tracks": "[0, 2]", "from_time": 16 }))))
            .unwrap();
        assert_eq!(params["arm_tracks"], json!([0, 2]));

        let stop = tool("stop_arrangement_recording");
        assert!(!stop.destructive && !stop.read_only);
        let params = stop.build_params(None).unwrap();
        assert_eq!(params["stop_playback"], json!(true));
        assert_eq!(params["disarm"], json!(false));
    }

    #[test]
    fn take_lanes_are_listed_read_only_and_created_with_a_name() {
        assert!(tool("get_take_lanes").read_only);
        let create = tool("create_take_lane");
        assert!(!create.read_only && !create.destructive);
        let params = create
            .build_params(Some(&args(json!({ "track_index": 3, "name": "Alt" }))))
            .unwrap();
        assert_eq!(params["name"], json!("Alt"));
    }

    #[test]
    fn clip_tools_reach_take_lane_clips() {
        let all = crate::tools::all();
        for name in ["get_clip", "get_clip_notes", "set_clip_properties", "clear_clip_envelope",
                     "delete_clip", "delete_arrangement_clip", "get_arrangement_clips"] {
            let def = all.iter().find(|t| t.name == name).expect("tool exists");
            let mut given = args(json!({
                "track_index": 0, "clip_index": 1, "view": "arrangement",
                "take_lane_index": 2, "device_index": 0, "parameter_index": 0
            }));
            given.retain(|key, _| def.args.iter().any(|a| a.name == key.as_str()));
            let params = def.build_params(Some(&given)).unwrap();
            assert_eq!(params["take_lane_index"], json!(2), "{name}");
        }
    }
}
