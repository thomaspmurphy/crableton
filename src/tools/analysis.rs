use serde_json::json;

use crate::mcp::ToolDef;

/// Sentinel commands answered by [`crate::analysis`] rather than forwarded to
/// Live. They gather a snapshot themselves, so the bulky payload never reaches
/// the model.
pub const HARMONY: &str = "__local_analyze_harmony";
pub const ARRANGEMENT: &str = "__local_analyze_arrangement";
pub const MIX: &str = "__local_analyze_mix";
pub const SAMPLE: &str = "__local_analyze_sample";

/// Remote Script commands no tool forwards to directly: local tools call them
/// to gather what they analyse.
pub const REMOTE_HELPERS: &[&str] = &["get_simpler_sample"];

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "analyze_harmony",
            HARMONY,
            "Work out what the MIDI in the set actually is, harmonically: the estimated key \
             with a confidence and whether it is ambiguous, the chord sounding in each bar, \
             which pitches carry the most weight, and any notes falling outside the estimated \
             scale. Use it before writing parts that have to sit with what is already there, \
             it is the difference between guessing a key and knowing it.",
        )
        .read_only()
        .opt_int(
            "track_index",
            "Narrow the analysis to one track. Omit to analyse every track together, which is \
             what you want for the set's key and chord progression.",
        )
        .opt_int("max_bars", "How many bars of chords to report, 1 to 256.")
        .default(json!(64)),

        ToolDef::new(
            "analyze_arrangement",
            ARRANGEMENT,
            "Map the structure of the Arrangement timeline: sections inferred from which \
             tracks are actually playing, the bar where each transition happens with what \
             enters and exits, per-track entry and coverage, and a per-bar density count. \
             Sections come from the instrumentation in the data, not from genre assumptions. \
             Use it to see the shape of a track you did not write, or to check a pass did \
             what you intended.",
        )
        .read_only()
        .opt_int(
            "min_section_bars",
            "Runs shorter than this are folded into the previous section, so a clip ending a \
             bar early does not read as a new section.",
        )
        .default(json!(2)),

        ToolDef::new(
            "analyze_mix",
            MIX,
            "Measure how the parts sit against each other: each track's pitch range and \
             duration-weighted centre, pairs of tracks competing for the same register, fader \
             positions in decibels, panning, and active sends. This is the measurable half of \
             a mix judgement. It can tell you five pads are stacked in two octaves, which is \
             usually why they sound congested, but it cannot hear the result.",
        )
        .read_only(),

        ToolDef::new(
            "get_meters",
            "get_meters",
            "Listen to the mix the only way available: sample the output meter of every \
             regular, return and master track for `duration` seconds and report each one's \
             average and peak (plus left and right peaks), with the song time covered. Run \
             it while the set plays. Levels are Live's 0 to 1 meter scale, not dBFS, and the \
             meters hold peaks briefly, so averages read high: compare tracks with each other \
             rather than against a number. Not available in `batch`.",
        )
        .read_only()
        .opt_num("duration", "Seconds to sample, up to 30.")
        .default(json!(2.0))
        .opt_num(
            "interval",
            "Seconds between samples. Live's tick is about 0.1 s, the finest available.",
        )
        .default(json!(0.1)),

        ToolDef::new(
            "probe_entry",
            "probe_entry",
            "Find what enters at a point in the Arrangement: move the playhead to `beat` minus \
             `preroll`, play across `beat`, sample every track's meter, and report each \
             track's level before and after, flagging tracks that enter, rise or drop out. \
             The playhead (and transport, if it was stopped) is restored afterwards. Use it \
             to find which part causes a clash heard only at one bar. Starts playback, so \
             the user hears it. Not available in `batch`.",
        )
        .num("beat", "The beat to probe, e.g. 64 for bar 17 in 4/4.")
        .opt_num("preroll", "Beats to play before `beat`.")
        .default(json!(5.0))
        .opt_num("after", "Beats to play after `beat`.")
        .default(json!(4.0))
        .opt_num("interval", "Seconds between samples, 0.1 at the finest.")
        .default(json!(0.1)),

        ToolDef::new(
            "analyze_sample",
            SAMPLE,
            "Measure a sample's pitch, length and level, and say how to tune it to the set. \
             Point it at a Simpler (`track_index` + `device_index`) or give a file `path` \
             (WAV or AIFF). It reads the file server-side and reports the fundamental in \
             several windows (attack and body of a hit often differ) as Hz, nearest note and \
             cents, plus length, peak and RMS. Using the set's root and scale, it gives the \
             Simpler Transpose and Detune that put the body pitch on the nearest root or \
             fifth. Use it when a kick or bass sample sounds out of tune against the bass line.",
        )
        .read_only()
        .opt_int("track_index", "Track holding the Simpler. Omit when passing `path`.")
        .opt_text("track_type", "Which list `track_index` indexes into.")
        .choices(&["regular", "return", "master"])
        .default(json!("regular"))
        .opt_text("expect_track_name", crate::tools::EXPECT_TRACK_NAME)
        .opt_int("device_index", "The Simpler's index in the track's device chain.")
        .opt_text(
            "chain_path",
            "Reach a Simpler nested in a rack, as dot-separated chain/device indices \
             relative to `device_index` (e.g. \"0.0\").",
        )
        .opt_text("path", "Absolute path of a WAV or AIFF file, instead of a Simpler.")
        .opt_int(
            "root_note",
            "Pitch class to tune to, 0 = C to 11 = B. Defaults to the set's root note.",
        ),
    ]
}
