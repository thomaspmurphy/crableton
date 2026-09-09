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
        .track_ref(),

        ToolDef::new(
            "duplicate_clip_to_arrangement",
            "duplicate_clip_to_arrangement",
            "Copy a Session clip into the Arrangement timeline on the same track. Use `repeats` \
             to lay down a run of consecutive copies: a 4-bar loop across 32 bars is one call, \
             not eight.",
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
        .int("clip_index", "Position in the track's Arrangement, as listed by `get_arrangement_clips`."),

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
        .opt_num("from_time", "Only delete clips starting at or after this beat.")
        .opt_num("to_time", "Only delete clips starting before this beat."),

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
             position. Useful for marking out sections before arranging.",
        )
        .text("name", "Locator label, e.g. \"Chorus\" or \"Drop\".")
        .num("time", "Position in beats from the start of the Arrangement. In 4/4, bar 17 is beat 64."),

        ToolDef::new("delete_locator", "delete_locator", "Delete one Arrangement locator.")
            .destructive()
            .int("index", "0-based index, as listed by `get_locators`."),

        ToolDef::new(
            "clear_locators",
            "clear_locators",
            "Delete every Arrangement locator, or all of those within a beat range.",
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
