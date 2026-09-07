use serde_json::json;

use crate::mcp::ToolDef;

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_scenes",
            "get_scenes",
            "List every scene with its name, colour, and per-scene tempo and time signature \
             where those are enabled.",
        )
        .read_only(),

        ToolDef::new("create_scene", "create_scene", "Insert an empty scene.")
            .opt_int("index", "Where to insert it. -1 appends to the end.")
            .default(json!(-1)),

        ToolDef::new("delete_scene", "delete_scene", "Delete a scene and every clip in it.")
            .destructive()
            .int("index", "0-based scene index."),

        ToolDef::new(
            "duplicate_scene",
            "duplicate_scene",
            "Duplicate a scene and its clips, inserting the copy directly below.",
        )
        .int("index", "0-based scene index."),

        ToolDef::new(
            "capture_and_insert_scene",
            "capture_and_insert_scene",
            "Capture whatever is currently playing into a new scene below the selected one — \
             Live's Capture and Insert Scene. The fastest way to commit a good-sounding \
             combination of clips.",
        ),

        ToolDef::new("fire_scene", "fire_scene", "Launch a scene, starting every clip in it.")
            .int("index", "0-based scene index."),

        ToolDef::new(
            "set_scene_properties",
            "set_scene_properties",
            "Rename, recolour, or give a scene its own tempo and time signature. Omit anything \
             you do not want to change.",
        )
        .int("index", "0-based scene index.")
        .opt_text("name", "New scene name. An empty string clears it.")
        .opt_int("color", "Colour as 0xRRGGBB, e.g. 16711680 for red.")
        .opt_num("tempo", "Tempo this scene switches the set to, in BPM.")
        .opt_bool("is_tempo_enabled", "Whether the scene applies its tempo when launched.")
        .opt_int("time_signature_numerator", "Beats per bar for this scene.")
        .opt_int("time_signature_denominator", "Beat unit for this scene: 1, 2, 4, 8, 16, 32 or 64.")
        .opt_bool("is_time_signature_enabled", "Whether the scene applies its time signature when launched."),
    ]
}
