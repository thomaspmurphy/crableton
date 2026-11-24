//! The tool surface, grouped by the part of Live each set drives.

use serde_json::json;

use crate::mcp::ToolDef;

pub mod analysis;
mod arrangement;
mod browser;
mod clip;
mod device;
mod meta;
mod note;
mod scene;
mod song;
mod track;

/// What `expect_track_name` tells the model. Shared by the tools that declare
/// `track_index` by hand, so every guard reads the same.
pub(crate) const EXPECT_TRACK_NAME: &str =
    "Guard against the set changing under you: the track's name as you last read it \
     alongside `track_index`. If the track at that index now has another name (the user \
     inserted, moved or deleted a track), the call fails before changing anything and says \
     where the name went. Pass it on every write. Return tracks match with or without \
     Live's letter prefix (\"Reverb\" or \"A-Reverb\").";

/// Argument shapes shared across domains, so "which track" means exactly the
/// same thing on a mixer call as on a device call.
pub(crate) trait CommonArgs: Sized {
    /// `track_index` plus the list it indexes into.
    fn track_ref(self) -> Self;
    /// A track reference plus a clip slot / arrangement position.
    fn clip_ref(self) -> Self;
    /// A track reference plus a device, including devices nested in racks.
    fn device_ref(self) -> Self;
}

impl CommonArgs for ToolDef {
    fn track_ref(self) -> Self {
        self.int("track_index", "0-based track index.")
            .opt_text(
                "track_type",
                "Which list `track_index` indexes into. Ignored for \"master\".",
            )
            .choices(&["regular", "return", "master"])
            .default(json!("regular"))
            .opt_text("expect_track_name", EXPECT_TRACK_NAME)
    }

    fn clip_ref(self) -> Self {
        self.track_ref()
            .int(
                "clip_index",
                "In the Session view, the clip slot index. In the Arrangement, \
                 the clip's position in the track's timeline (as listed by \
                 `get_arrangement_clips`).",
            )
            .opt_text("view", "Which view the clip lives in.")
            .choices(&["session", "arrangement"])
            .default(json!("session"))
            .opt_int(
                "take_lane_index",
                "Arrangement only: reach a clip on one of the track's take lanes, as listed \
                 by `get_take_lanes`, instead of its main lane. `clip_index` then counts \
                 that lane's clips.",
            )
    }

    fn device_ref(self) -> Self {
        self.track_ref()
            .int("device_index", "0-based index into the track's device chain.")
            .opt_text(
                "chain_path",
                "Reach a device nested inside a rack, as dot-separated \
                 chain/device indices relative to `device_index`. For example \
                 \"0.2\" is the third device of the rack's first chain, and \
                 \"0.2.1.0\" descends one rack further.",
            )
    }
}

fn tagged(group: &'static str, defs: Vec<ToolDef>) -> impl Iterator<Item = ToolDef> {
    defs.into_iter().map(move |mut def| {
        def.group = group;
        def
    })
}

/// Every tool crableton knows how to serve.
pub fn all() -> Vec<ToolDef> {
    tagged("meta", meta::tools())
        .chain(tagged("song", song::tools()))
        .chain(tagged("scene", scene::tools()))
        .chain(tagged("track", track::tools()))
        .chain(tagged("clip", clip::tools()))
        .chain(tagged("note", note::tools()))
        .chain(tagged("device", device::tools()))
        .chain(tagged("browser", browser::tools()))
        .chain(tagged("arrangement", arrangement::tools()))
        .chain(tagged("analysis", analysis::tools()))
        .collect()
}

/// The groups that can be switched off via `CRABLETON_TOOLSETS`.
pub const GROUPS: &[&str] = &[
    "meta",
    "song",
    "scene",
    "track",
    "clip",
    "note",
    "device",
    "browser",
    "arrangement",
    "analysis",
];

/// Filter [`all`] down to a comma-separated set of groups. `meta` is always
/// kept, without it a client cannot check which Remote Script it is talking to.
pub fn selected(spec: Option<&str>) -> Vec<ToolDef> {
    let Some(spec) = spec.map(str::trim).filter(|s| !s.is_empty() && *s != "all") else {
        return all();
    };
    let wanted: Vec<&str> = spec.split(',').map(str::trim).filter(|s| !s.is_empty()).collect();
    all()
        .into_iter()
        .filter(|def| def.group == "meta" || wanted.contains(&def.group))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn tool_names_are_unique_and_snake_case() {
        let mut seen = HashSet::new();
        for def in all() {
            assert!(seen.insert(def.name), "duplicate tool `{}`", def.name);
            assert!(
                def.name
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_'),
                "`{}` is not snake_case",
                def.name
            );
        }
    }

    #[test]
    fn every_group_is_declared_and_populated() {
        let used: HashSet<&str> = all().iter().map(|d| d.group).collect();
        for group in GROUPS {
            assert!(used.contains(group), "group `{group}` has no tools");
        }
        for group in &used {
            assert!(GROUPS.contains(group), "group `{group}` missing from GROUPS");
        }
    }

    #[test]
    fn every_tool_addressing_a_track_accepts_the_name_guard() {
        for def in all() {
            // Analysis tools read a snapshot crableton takes itself; they never
            // write, so there is nothing to guard.
            if def.group == "analysis" {
                continue;
            }
            let declares = |name: &str| def.args.iter().any(|a| a.name == name);
            if declares("track_index") {
                assert!(declares("expect_track_name"), "`{}` lacks expect_track_name", def.name);
            }
            if declares("target_track_index") {
                assert!(
                    declares("expect_target_track_name"),
                    "`{}` lacks expect_target_track_name",
                    def.name
                );
            }
        }
    }

    #[test]
    fn clip_and_device_tools_forward_the_name_guard() {
        let tools = all();
        for (tool, args) in [
            ("fire_clip", json!({ "track_index": 2, "clip_index": 0, "expect_track_name": "Keys" })),
            (
                "duplicate_clip",
                json!({ "track_index": 2, "clip_index": 0, "target_track_index": 3,
                        "target_clip_index": 1, "expect_track_name": "Keys",
                        "expect_target_track_name": "Bass" }),
            ),
            ("delete_device", json!({ "track_index": 2, "device_index": 0, "expect_track_name": "Keys" })),
        ] {
            let def = tools.iter().find(|d| d.name == tool).expect(tool);
            let params = def
                .build_params(args.as_object())
                .unwrap_or_else(|e| panic!("{tool}: {e}"));
            assert_eq!(params["expect_track_name"], "Keys", "{tool}");
        }
    }

    #[test]
    fn selecting_groups_always_keeps_meta() {
        let names: HashSet<&str> = selected(Some("song")).iter().map(|d| d.name).collect();
        assert!(names.contains("set_tempo"));
        assert!(names.contains("get_remote_script_info"));
        assert!(!names.contains("create_midi_track"));
        assert_eq!(selected(Some("all")).len(), all().len());
        assert_eq!(selected(None).len(), all().len());
    }
}
