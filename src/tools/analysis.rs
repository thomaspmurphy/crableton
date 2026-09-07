use serde_json::json;

use crate::mcp::ToolDef;

/// Sentinel commands answered by [`crate::analysis`] rather than forwarded to
/// Live. They gather a snapshot themselves, so the bulky payload never reaches
/// the model.
pub const HARMONY: &str = "__local_analyze_harmony";
pub const ARRANGEMENT: &str = "__local_analyze_arrangement";
pub const MIX: &str = "__local_analyze_mix";

pub fn tools() -> Vec<ToolDef> {
    vec![
        ToolDef::new(
            "analyze_harmony",
            HARMONY,
            "Work out what the MIDI in the set actually is, harmonically: the estimated key \
             with a confidence and whether it is ambiguous, the chord sounding in each bar, \
             which pitches carry the most weight, and any notes falling outside the estimated \
             scale. Use it before writing parts that have to sit with what is already there — \
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
             a mix judgement — it can tell you five pads are stacked in two octaves, which is \
             usually why they sound congested, but it cannot hear the result.",
        )
        .read_only(),
    ]
}
