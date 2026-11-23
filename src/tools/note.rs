use serde_json::json;

use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

/// The four optional window arguments shared by the note read, clear and edit tools.
fn note_window(def: ToolDef) -> ToolDef {
    def.opt_num("from_time", "Start of the time window, in beats from the clip origin.")
        .opt_num("time_span", "Length of the time window in beats. Omit for the whole clip.")
        .opt_int("from_pitch", "Lowest MIDI note number in the window, 0 to 127.")
        .opt_int("pitch_span", "How many semitones the window covers. Omit for all pitches.")
}

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "get_clip_notes",
            "get_clip_notes",
            "Read the MIDI notes of a clip: pitch, start time, duration, velocity and mute, \
             plus per-note probability, velocity deviation and release velocity where Live \
             supports them. Optionally restrict to a window of time and pitch, useful for \
             reading one bar or one drum lane out of a busy clip.",
        )
        .read_only()
        .clip_ref()
        .pipe(note_window),

        ToolDef::new(
            "add_notes_to_clip",
            "add_notes_to_clip",
            "Add MIDI notes to a clip. This appends: existing notes are left alone, so calling \
             it twice with the same notes produces duplicates. Use `replace_clip_notes` to \
             rewrite a clip.",
        )
        .clip_ref()
        .notes("notes", "Notes to add."),

        ToolDef::new(
            "replace_clip_notes",
            "replace_clip_notes",
            "Replace a clip's notes with a new set, in one atomic step. This is the tool to use \
             for editing: read with `get_clip_notes`, change the list, write it back. Doing it \
             as clear-then-add instead leaves the clip briefly empty and costs an extra round \
             trip.",
        )
        .destructive()
        .clip_ref()
        .notes("notes", "The complete new contents of the clip. An empty list clears it."),

        ToolDef::new(
            "clear_notes_from_clip",
            "clear_notes_from_clip",
            "Delete notes from a clip. With no window arguments it clears the whole clip; with \
             them it deletes only the notes inside that range of time and pitch.",
        )
        .destructive()
        .clip_ref()
        .pipe(note_window),

        ToolDef::new(
            "transpose_notes",
            "transpose_notes",
            "Shift the pitch of a clip's notes by a number of semitones, keeping their timing, \
             velocity, note ids and per-note probability. With no window it moves the whole clip; \
             with one it moves only the notes inside that range of time and pitch, for example \
             one bar or the bass register. If any note would land outside 0 to 127 nothing \
             changes and the error names those notes. Prefer this over rewriting the clip with \
             `replace_clip_notes` when only pitch changes.",
        )
        .destructive()
        .clip_ref()
        .int("semitones", "Semitones to shift by: 12 is an octave up, -7 a fifth down.")
        .pipe(note_window),

        ToolDef::new(
            "duplicate_notes",
            "duplicate_notes",
            "Copy the notes in a window of a clip to a later (or earlier) point in the same \
             clip, optionally transposed: repeat a bar, double a phrase, or stack a part an \
             octave up. The originals stay, and copies that land on existing notes of the same \
             pitch collide with them as any added note would. When the copies reach past the end of a Session clip, \
             its loop end (or end marker if it is not looping) is moved out to the next whole \
             bar so they play; pass `extend_clip: false` to leave the length alone. \
             Arrangement clips are never lengthened. To copy notes to a different clip, read \
             them with `get_clip_notes` and use `add_notes_to_clip`.",
        )
        .clip_ref()
        .num(
            "time_offset",
            "Beats to move the copies by, relative to the originals. 4 copies a bar of 4/4 \
             onto the next bar.",
        )
        .opt_int("pitch_offset", "Semitones to transpose the copies by.")
        .default(json!(0))
        .opt_bool(
            "extend_clip",
            "Lengthen a Session clip so copies past its end are heard.",
        )
        .default(json!(true))
        .pipe(note_window),

        ToolDef::new(
            "modify_notes",
            "modify_notes",
            "Edit existing notes in place by note id, changing only the fields given: pitch, \
             start time, duration, velocity, mute, probability, velocity deviation, release \
             velocity. Get ids from `get_clip_notes`. Other notes and unchanged fields are left \
             alone and the notes keep their ids, so this is the precise tool for nudging a few \
             notes or shaping velocities. Use `transpose_notes` to shift a whole range and \
             `replace_clip_notes` to rewrite the clip. Needs Live 11 or newer; unknown ids are \
             rejected before anything changes.",
        )
        .destructive()
        .clip_ref()
        .note_edits("changes", "One entry per note to change: its `note_id` plus the new values."),

        ToolDef::new(
            "remove_notes_by_id",
            "remove_notes_by_id",
            "Delete specific notes by note id, as returned by `get_clip_notes`. Use this to \
             pick out individual notes, such as a stray hit or one note of a chord; use \
             `clear_notes_from_clip` to delete everything in a range of time and pitch. Needs \
             Live 11 or newer; unknown ids are rejected before anything is deleted.",
        )
        .destructive()
        .clip_ref()
        .int_list("note_ids", "Ids of the notes to delete."),
    ]
}

/// Small helper so a shared block of arguments reads as one step in the chain.
trait Pipe: Sized {
    fn pipe<F: FnOnce(Self) -> Self>(self, f: F) -> Self {
        f(self)
    }
}

impl Pipe for ToolDef {}

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
    fn editing_tools_that_can_lose_notes_are_destructive() {
        for name in ["transpose_notes", "modify_notes", "remove_notes_by_id"] {
            assert!(tool(name).destructive, "{name} should be destructive");
        }
        let duplicate = tool("duplicate_notes");
        assert!(!duplicate.destructive && !duplicate.read_only);
    }

    #[test]
    fn duplicate_notes_defaults_to_extending_the_clip() {
        let params = tool("duplicate_notes")
            .build_params(Some(&args(json!({ "track_index": 0, "clip_index": 0, "time_offset": 4 }))))
            .unwrap();
        assert_eq!(params["extend_clip"], json!(true));
        assert_eq!(params["pitch_offset"], json!(0));
    }

    #[test]
    fn modify_notes_schema_requires_a_note_id_per_change() {
        let schema = tool("modify_notes").input_schema();
        let item = &schema["properties"]["changes"]["items"];
        assert_eq!(item["required"], json!(["note_id"]));
        assert!(item["properties"]["release_velocity"].is_object());

        let params = tool("modify_notes")
            .build_params(Some(&args(json!({
                "track_index": 0, "clip_index": 0,
                "changes": "[{\"note_id\": 3, \"velocity\": 90}]"
            }))))
            .unwrap();
        assert_eq!(params["changes"][0]["note_id"], json!(3));
    }

    #[test]
    fn remove_notes_by_id_requires_ids() {
        let err = tool("remove_notes_by_id")
            .build_params(Some(&args(json!({ "track_index": 0, "clip_index": 0 }))))
            .unwrap_err();
        assert!(err.contains("note_ids"));
    }
}
