use crate::mcp::ToolDef;
use crate::tools::CommonArgs;

/// The four optional window arguments shared by the note read and clear tools.
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
    ]
}

/// Small helper so a shared block of arguments reads as one step in the chain.
trait Pipe: Sized {
    fn pipe<F: FnOnce(Self) -> Self>(self, f: F) -> Self {
        f(self)
    }
}

impl Pipe for ToolDef {}
