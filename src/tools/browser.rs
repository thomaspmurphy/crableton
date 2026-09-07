use serde_json::json;

use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

const CATEGORIES: &[&str] = &[
    "all",
    "instruments",
    "sounds",
    "drums",
    "audio_effects",
    "midi_effects",
    "plugins",
    "max_for_live",
    "samples",
    "packs",
    "user_library",
];

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "search_browser",
            "search_browser",
            "Find loadable things in Live's browser by name — instruments, presets, samples, \
             effects and racks — and get back the URIs `load_device` needs. This is almost \
             always the right way in: walking the tree by hand is slow and the library layout \
             varies between installations.",
        )
        .read_only()
        .text("query", "What to look for, e.g. \"analog bass\", \"tape delay\", \"808\".")
        .opt_text("category_type", "Restrict the search to one part of the browser.")
        .choices(CATEGORIES)
        .default(json!("all"))
        .opt_int("limit", "Maximum results to return, 1 to 200.")
        .default(json!(30)),

        ToolDef::new(
            "get_browser_tree",
            "get_browser_tree",
            "Walk Live's browser as a tree of folders. Slow on a large library, and cached \
             between calls — prefer `search_browser` unless you specifically need the layout.",
        )
        .read_only()
        .opt_text("category_type", "Which part of the browser to show.")
        .choices(CATEGORIES)
        .default(json!("all")),

        ToolDef::new(
            "get_browser_items_at_path",
            "get_browser_items_at_path",
            "List what sits at one path in Live's browser, with each item's URI and whether it \
             can be loaded.",
        )
        .read_only()
        .text("path", "Browser path such as \"instruments/Wavetable\" or \"drums/Drum Rack\"."),

        ToolDef::new(
            "load_device",
            "load_browser_item",
            "Load an instrument, effect, preset or sample onto a track, using a URI from \
             `search_browser` or `get_browser_items_at_path`. It is appended to the end of the \
             track's device chain.",
        )
        .track_ref()
        .text("uri", "Browser item URI."),

        ToolDef::new(
            "load_drum_kit",
            "load_drum_kit",
            "Load a Drum Rack onto a track and fill it with a kit in one step.",
        )
        .track_ref()
        .text("rack_uri", "URI of the Drum Rack to load.")
        .text("kit_path", "Browser path holding the kit to load into it, e.g. \"drums/acoustic\"."),
    ]
}
