//! Harmonic analysis of MIDI notes.
//!
//! Reports what the notes *are*: pitch classes, chords and the best-fitting key,
//! and leaves what to do about it to the caller. A model driving Live cannot
//! hear the set, but it can reason about this perfectly well once someone has
//! done the counting.

use serde_json::{Value, json};

use super::{Note, PITCH_NAMES};

/// Krumhansl-Kessler key profiles: how strongly each scale degree is weighted
/// in major and minor tonal music. Correlating a set's pitch-class histogram
/// against all 24 rotations is the standard way to estimate a key, and it
/// degrades gracefully on ambiguous material rather than guessing wildly.
const MAJOR_PROFILE: [f64; 12] = [
    6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88,
];
const MINOR_PROFILE: [f64; 12] = [
    6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17,
];

/// Chord templates as semitone offsets from the root, ordered so that richer
/// spellings win over the triads they contain.
const CHORD_TEMPLATES: &[(&str, &[u8])] = &[
    ("maj7", &[0, 4, 7, 11]),
    ("min7", &[0, 3, 7, 10]),
    ("7", &[0, 4, 7, 10]),
    ("min7b5", &[0, 3, 6, 10]),
    ("dim7", &[0, 3, 6, 9]),
    ("minmaj7", &[0, 3, 7, 11]),
    ("6", &[0, 4, 7, 9]),
    ("min6", &[0, 3, 7, 9]),
    ("add9", &[0, 4, 7, 2]),
    ("maj", &[0, 4, 7]),
    ("min", &[0, 3, 7]),
    ("dim", &[0, 3, 6]),
    ("aug", &[0, 4, 8]),
    ("sus4", &[0, 5, 7]),
    ("sus2", &[0, 2, 7]),
    ("5", &[0, 7]),
];

/// A pitch-class histogram weighted by how long each note sounds.
///
/// Duration-weighted rather than counted: a passing sixteenth should not carry
/// the same harmonic weight as a whole-bar pad.
pub fn pitch_class_weights(notes: &[Note]) -> [f64; 12] {
    let mut weights = [0.0; 12];
    for note in notes {
        if note.muted {
            continue;
        }
        weights[(note.pitch % 12) as usize] += note.duration.max(0.01);
    }
    weights
}

#[derive(Debug, Clone)]
pub struct KeyEstimate {
    pub tonic: u8,
    pub is_minor: bool,
    pub correlation: f64,
    /// How far ahead of the runner-up this estimate sits. Small margins mean
    /// the material is genuinely ambiguous, which is worth saying out loud.
    pub margin: f64,
}

impl KeyEstimate {
    pub fn name(&self) -> String {
        format!(
            "{} {}",
            PITCH_NAMES[self.tonic as usize],
            if self.is_minor { "minor" } else { "major" }
        )
    }

    /// Scale degrees of this key, as pitch classes.
    ///
    /// Only the major and minor readings. For modal material use
    /// [`identify_scale`], which derives the collection from what is actually
    /// played instead of assuming one of these two.
    pub fn scale(&self) -> Vec<u8> {
        let steps: &[u8] = if self.is_minor {
            &[0, 2, 3, 5, 7, 8, 10]
        } else {
            &[0, 2, 4, 5, 7, 9, 11]
        };
        steps.iter().map(|s| (self.tonic + s) % 12).collect()
    }
}

/// A scale collection and the names of its rotations.
///
/// The correlation above can only ever answer "major" or "minor", so a Dorian
/// set gets fitted to the nearest minor and its characteristic sixth reported
/// as foreign. Matching the played pitch classes against these collections
/// instead names the mode from what is there.
struct Collection {
    name: &'static str,
    /// Semitones above the collection's own root.
    intervals: &'static [u8],
    /// `(semitones of the tonic above the root, mode name, how commonly used)`.
    /// The rank breaks ties when a set fits several collections; lower is more
    /// common, which is a prior, not a fact about the music.
    modes: &'static [(u8, &'static str, u8)],
}

const COLLECTIONS: &[Collection] = &[
    Collection {
        name: "diatonic",
        intervals: &[0, 2, 4, 5, 7, 9, 11],
        modes: &[
            (0, "Ionian (major)", 0),
            (9, "Aeolian (natural minor)", 1),
            (2, "Dorian", 2),
            (7, "Mixolydian", 3),
            (5, "Lydian", 4),
            (4, "Phrygian", 5),
            (11, "Locrian", 6),
        ],
    },
    Collection {
        name: "pentatonic",
        intervals: &[0, 2, 4, 7, 9],
        modes: &[(0, "major pentatonic", 7), (9, "minor pentatonic", 8)],
    },
    Collection {
        name: "blues",
        intervals: &[0, 3, 5, 6, 7, 10],
        modes: &[(0, "blues", 9)],
    },
    Collection {
        name: "harmonic minor",
        intervals: &[0, 2, 3, 5, 7, 8, 11],
        modes: &[
            (0, "harmonic minor", 10),
            (7, "Phrygian dominant", 11),
            (5, "Ukrainian Dorian", 18),
        ],
    },
    Collection {
        name: "melodic minor",
        intervals: &[0, 2, 3, 5, 7, 9, 11],
        modes: &[
            (0, "melodic minor", 12),
            (5, "Lydian dominant", 13),
            (11, "altered", 14),
        ],
    },
    Collection {
        name: "whole tone",
        intervals: &[0, 2, 4, 6, 8, 10],
        modes: &[(0, "whole tone", 19)],
    },
];

/// A named scale derived from the notes actually played.
#[derive(Debug, Clone)]
pub struct ScaleEstimate {
    pub tonic: u8,
    /// e.g. "Dorian", or "mode of harmonic minor" where the rotation has no
    /// name worth quoting.
    pub mode: String,
    /// e.g. "diatonic".
    pub collection: &'static str,
    /// Pitch classes of the scale, tonic first.
    pub scale: Vec<u8>,
    /// Played notes that fall outside the collection: chromatic colour, or a
    /// sign the reading is wrong.
    pub chromatic: Vec<u8>,
    /// True when the played set fills the collection exactly, so the rotation
    /// is determined rather than inferred.
    pub exact: bool,
    /// Other readings that also contain every played note.
    pub alternatives: Vec<String>,
}

impl ScaleEstimate {
    pub fn name(&self) -> String {
        format!("{} {}", PITCH_NAMES[self.tonic as usize], self.mode)
    }
}

fn rotate(root: u8, intervals: &[u8]) -> Vec<u8> {
    intervals.iter().map(|s| (root + s) % 12).collect()
}

/// Name the scale formed by the played pitch classes around a known tonic.
///
/// The tonic comes from the correlation, which is reliable; only the mode is
/// in question. Candidates are ranked by how many played notes fall outside
/// the collection, then by how much of the collection is unaccounted for, so
/// an exact fit always beats a superset that merely contains the notes.
pub fn identify_scale(weights: &[f64; 12], tonic: u8) -> Option<ScaleEstimate> {
    let played: Vec<u8> = (0..12u8).filter(|&pc| weights[pc as usize] > 0.0).collect();
    if played.is_empty() {
        return None;
    }

    struct Candidate {
        outside: Vec<u8>,
        unfilled: usize,
        rank: u8,
        mode: String,
        collection: &'static str,
        scale: Vec<u8>,
    }

    let mut candidates: Vec<Candidate> = Vec::new();
    for collection in COLLECTIONS {
        for root in 0..12u8 {
            let pitches = rotate(root, collection.intervals);
            // The tonic has to belong to the collection, or naming a rotation
            // around it is meaningless.
            if !pitches.contains(&tonic) {
                continue;
            }
            let offset = (tonic + 12 - root) % 12;
            let named = collection.modes.iter().find(|(o, _, _)| *o == offset);
            let (mode, rank) = match named {
                Some((_, name, rank)) => ((*name).to_string(), *rank),
                // An unnamed rotation is still a true description, just a less
                // useful one, so keep it but rank it last.
                None => (
                    format!(
                        "mode {} of {}",
                        collection.intervals.iter().position(|s| *s == offset).map_or(0, |i| i + 1),
                        collection.name
                    ),
                    30,
                ),
            };

            let outside: Vec<u8> = played.iter().copied().filter(|pc| !pitches.contains(pc)).collect();
            let unfilled = pitches.iter().filter(|pc| !played.contains(pc)).count();

            // Rotate the scale so it reads from the tonic.
            let mut scale = pitches.clone();
            scale.sort_by_key(|pc| (pc + 12 - tonic) % 12);

            candidates.push(Candidate {
                outside,
                unfilled,
                rank,
                mode,
                collection: collection.name,
                scale,
            });
        }
    }

    candidates.sort_by_key(|c| (c.outside.len(), c.unfilled, c.rank));
    let best = candidates.first()?;

    // Readings that account for every played note just as well, which is what
    // makes a five-note set genuinely underdetermined.
    let alternatives: Vec<String> = candidates
        .iter()
        .skip(1)
        .filter(|c| c.outside.len() == best.outside.len() && c.mode != best.mode)
        .map(|c| format!("{} {}", PITCH_NAMES[tonic as usize], c.mode))
        .take(4)
        .collect();

    Some(ScaleEstimate {
        tonic,
        mode: best.mode.clone(),
        collection: best.collection,
        scale: best.scale.clone(),
        chromatic: best.outside.clone(),
        exact: best.outside.is_empty() && best.unfilled == 0,
        alternatives,
    })
}

/// Estimate the key by correlating the histogram against all 24 profiles.
pub fn estimate_key(weights: &[f64; 12]) -> Option<KeyEstimate> {
    let total: f64 = weights.iter().sum();
    if total <= 0.0 {
        return None;
    }

    let mut scored: Vec<(f64, u8, bool)> = Vec::with_capacity(24);
    for tonic in 0..12u8 {
        for (is_minor, profile) in [(false, &MAJOR_PROFILE), (true, &MINOR_PROFILE)] {
            let rotated: Vec<f64> = (0..12)
                .map(|i| profile[(i + 12 - tonic as usize) % 12])
                .collect();
            scored.push((correlation(weights, &rotated), tonic, is_minor));
        }
    }
    scored.sort_by(|a, b| b.0.total_cmp(&a.0));

    let (best, tonic, is_minor) = scored[0];
    Some(KeyEstimate {
        tonic,
        is_minor,
        correlation: best,
        margin: best - scored.get(1).map_or(best, |s| s.0),
    })
}

/// Pearson correlation, which is what makes the profiles scale-invariant.
fn correlation(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len() as f64;
    let mean_a = a.iter().sum::<f64>() / n;
    let mean_b = b.iter().sum::<f64>() / n;
    let mut covariance = 0.0;
    let mut var_a = 0.0;
    let mut var_b = 0.0;
    for (x, y) in a.iter().zip(b) {
        let dx = x - mean_a;
        let dy = y - mean_b;
        covariance += dx * dy;
        var_a += dx * dx;
        var_b += dy * dy;
    }
    let denominator = (var_a * var_b).sqrt();
    if denominator == 0.0 {
        0.0
    } else {
        covariance / denominator
    }
}

/// Name the chord formed by a set of pitch classes.
///
/// Scores every root against every template, rewarding matched template
/// degrees and penalising notes the template does not account for, so a dense
/// pad voicing still resolves to something sensible.
pub fn name_chord(pitch_classes: &[u8], bass: Option<u8>) -> Option<(String, f64)> {
    let present: Vec<u8> = {
        let mut seen = [false; 12];
        for &pc in pitch_classes {
            seen[(pc % 12) as usize] = true;
        }
        (0..12u8).filter(|&pc| seen[pc as usize]).collect()
    };
    if present.is_empty() {
        return None;
    }
    if present.len() == 1 {
        return Some((format!("{} (single note)", PITCH_NAMES[present[0] as usize]), 1.0));
    }

    let mut best: Option<(String, f64)> = None;
    for root in 0..12u8 {
        for (label, template) in CHORD_TEMPLATES {
            let wanted: Vec<u8> = template.iter().map(|s| (root + s) % 12).collect();
            let matched = wanted.iter().filter(|pc| present.contains(pc)).count();
            let extra = present.iter().filter(|pc| !wanted.contains(pc)).count();

            // Every template degree must be present; a "maj7" missing its
            // seventh is a triad, not a weak maj7.
            if matched < wanted.len() {
                continue;
            }
            let score = matched as f64 - 0.5 * extra as f64
                // Nudge towards roots actually sounding in the bass.
                + if bass.map(|b| b % 12) == Some(root) { 0.4 } else { 0.0 };

            if best.as_ref().is_none_or(|(_, s)| score > *s) {
                let mut name = format!("{}{}", PITCH_NAMES[root as usize], label);
                if let Some(b) = bass.map(|b| b % 12)
                    && b != root
                {
                    name.push('/');
                    name.push_str(PITCH_NAMES[b as usize]);
                }
                best = Some((name, score));
            }
        }
    }
    best
}

/// Slice notes into bars and name the chord sounding in each.
///
/// A note counts towards every bar it sounds through, so a sustained pad
/// contributes to the harmony of each bar it covers rather than only the one
/// it starts in.
pub fn chords_by_bar(notes: &[Note], beats_per_bar: f64, max_bars: usize) -> Vec<Value> {
    if notes.is_empty() || beats_per_bar <= 0.0 {
        return Vec::new();
    }

    let end = notes
        .iter()
        .map(|n| n.start + n.duration)
        .fold(0.0f64, f64::max);
    let bars = ((end / beats_per_bar).ceil() as usize).clamp(1, max_bars);

    (0..bars)
        .map(|bar| {
            let from = bar as f64 * beats_per_bar;
            let to = from + beats_per_bar;
            let sounding: Vec<&Note> = notes
                .iter()
                .filter(|n| !n.muted && n.start < to - 1e-9 && n.start + n.duration > from + 1e-9)
                .collect();

            if sounding.is_empty() {
                return json!({ "bar": bar + 1, "chord": null, "pitches": [] });
            }

            let pitch_classes: Vec<u8> = sounding.iter().map(|n| n.pitch % 12).collect();
            let bass = sounding.iter().map(|n| n.pitch).min().map(|p| p % 12);
            let named = name_chord(&pitch_classes, bass);

            json!({
                "bar": bar + 1,
                "chord": named.as_ref().map(|(name, _)| name.clone()),
                "confidence": named.as_ref().map(|(_, score)| (score * 100.0).round() / 100.0),
                "note_count": sounding.len(),
                "lowest_pitch": sounding.iter().map(|n| n.pitch).min(),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note(pitch: u8, start: f64, duration: f64) -> Note {
        Note {
            pitch,
            start,
            duration,
            velocity: 100.0,
            muted: false,
            track: 0,
            track_name: String::new(),
        }
    }

    #[test]
    fn names_common_chords() {
        // C E G
        assert_eq!(name_chord(&[0, 4, 7], None).unwrap().0, "Cmaj");
        // A C E
        assert_eq!(name_chord(&[9, 0, 4], None).unwrap().0, "Amin");
        // G B D F
        assert_eq!(name_chord(&[7, 11, 2, 5], None).unwrap().0, "G7");
        // D F A C
        assert_eq!(name_chord(&[2, 5, 9, 0], None).unwrap().0, "Dmin7");
        // C F G, sus4, not a weak triad
        assert_eq!(name_chord(&[0, 5, 7], None).unwrap().0, "Csus4");
    }

    #[test]
    fn reports_a_slash_chord_when_the_bass_is_not_the_root() {
        // C major triad over E in the bass.
        assert_eq!(name_chord(&[0, 4, 7], Some(4)).unwrap().0, "Cmaj/E");
    }

    #[test]
    fn a_fifth_is_not_promoted_to_a_triad() {
        assert_eq!(name_chord(&[0, 7], None).unwrap().0, "C5");
    }

    #[test]
    fn estimates_key_from_a_diatonic_melody() {
        // A C-major scale should read as C major, or at worst its relative minor.
        let notes: Vec<Note> = [60, 62, 64, 65, 67, 69, 71, 72]
            .iter()
            .enumerate()
            .map(|(i, &p)| note(p, i as f64, 1.0))
            .collect();
        let estimate = estimate_key(&pitch_class_weights(&notes)).unwrap();
        assert!(
            estimate.name() == "C major" || estimate.name() == "A minor",
            "got {}",
            estimate.name()
        );
    }

    #[test]
    fn duration_weighting_beats_note_counting() {
        // Many brief F#s against one long, sustained C-major triad: the triad
        // should still decide the key.
        let mut notes = vec![note(60, 0.0, 16.0), note(64, 0.0, 16.0), note(67, 0.0, 16.0)];
        for i in 0..8 {
            notes.push(note(66, i as f64, 0.05));
        }
        let weights = pitch_class_weights(&notes);
        assert!(weights[0] > weights[6], "long C should outweigh brief F#s");
    }

    #[test]
    fn key_estimation_is_none_for_silence() {
        assert!(estimate_key(&pitch_class_weights(&[])).is_none());
    }

    /// Build a weight table from pitch-class percentages.
    fn weights_of(pairs: &[(u8, f64)]) -> [f64; 12] {
        let mut weights = [0.0; 12];
        for &(pc, weight) in pairs {
            weights[pc as usize] = weight;
        }
        weights
    }

    #[test]
    fn names_dorian_rather_than_a_minor_key_with_a_wrong_sixth() {
        // The exact pitch content of a real 5/4 set: B C# D E F# G# A, no G.
        // The correlation calls this B minor and reports the G# as foreign,
        // when G# is precisely what makes it Dorian.
        let weights = weights_of(&[
            (11, 24.8), (6, 22.9), (2, 22.0), (1, 19.2), (8, 5.6), (4, 3.3), (9, 2.3),
        ]);
        let tonic = estimate_key(&weights).unwrap().tonic;
        assert_eq!(PITCH_NAMES[tonic as usize], "B", "the tonic was always right");

        let scale = identify_scale(&weights, tonic).unwrap();
        assert_eq!(scale.name(), "B Dorian");
        assert_eq!(scale.collection, "diatonic");
        assert!(scale.exact, "all seven degrees are played, so the mode is pinned down");
        assert!(scale.chromatic.is_empty(), "G# belongs to the scale, it is not an outlier");
        assert_eq!(
            scale.scale.iter().map(|&pc| PITCH_NAMES[pc as usize]).collect::<Vec<_>>(),
            ["B", "C#", "D", "E", "F#", "G#", "A"],
        );
    }

    #[test]
    fn names_each_diatonic_mode_from_its_own_notes() {
        // Every mode of the C-major collection, rooted on its own tonic.
        for (tonic, expected) in [
            (0u8, "C Ionian (major)"),
            (2, "D Dorian"),
            (4, "E Phrygian"),
            (5, "F Lydian"),
            (7, "G Mixolydian"),
            (9, "A Aeolian (natural minor)"),
            (11, "B Locrian"),
        ] {
            let weights = weights_of(&[
                (0, 1.0), (2, 1.0), (4, 1.0), (5, 1.0), (7, 1.0), (9, 1.0), (11, 1.0),
            ]);
            let scale = identify_scale(&weights, tonic).unwrap();
            assert_eq!(scale.name(), expected);
            assert!(scale.exact);
        }
    }

    #[test]
    fn a_partial_scale_is_reported_as_undetermined_with_alternatives() {
        // C E G only: consistent with several collections, so the mode cannot
        // be pinned down and saying "C major" outright would overreach.
        let weights = weights_of(&[(0, 3.0), (4, 2.0), (7, 2.0)]);
        let scale = identify_scale(&weights, 0).unwrap();
        assert!(!scale.exact, "three notes do not determine a seven-note mode");
        assert!(
            !scale.alternatives.is_empty(),
            "other readings fit these notes equally well"
        );
        assert!(scale.chromatic.is_empty(), "the notes themselves are all diatonic");
    }

    #[test]
    fn chromatic_notes_are_reported_rather_than_forced_into_a_scale() {
        // A C-major scale plus a chromatic F#, which no diatonic rotation
        // containing C, D, E, F, G, A and B can absorb.
        let weights = weights_of(&[
            (0, 5.0), (2, 2.0), (4, 3.0), (5, 2.0), (7, 3.0), (9, 2.0), (11, 2.0), (6, 0.5),
        ]);
        let scale = identify_scale(&weights, 0).unwrap();
        assert_eq!(
            scale.chromatic.iter().map(|&pc| PITCH_NAMES[pc as usize]).collect::<Vec<_>>(),
            ["F#"],
        );
        assert_eq!(scale.name(), "C Ionian (major)");
    }

    #[test]
    fn recognises_pentatonic_and_harmonic_minor_collections() {
        // Exactly the five notes of a major pentatonic beats calling it a
        // major scale with two degrees missing.
        let pentatonic = weights_of(&[(0, 2.0), (2, 1.0), (4, 1.0), (7, 1.0), (9, 1.0)]);
        let scale = identify_scale(&pentatonic, 0).unwrap();
        assert_eq!(scale.name(), "C major pentatonic");
        assert!(scale.exact);

        // A natural minor with a raised seventh is harmonic minor, not a
        // chromatic accident.
        let harmonic = weights_of(&[
            (9, 4.0), (11, 2.0), (0, 2.0), (2, 2.0), (4, 3.0), (5, 2.0), (8, 2.0),
        ]);
        let scale = identify_scale(&harmonic, 9).unwrap();
        assert_eq!(scale.name(), "A harmonic minor");
        assert!(scale.chromatic.is_empty());
    }

    #[test]
    fn the_tonic_must_belong_to_the_scale_it_names() {
        let weights = weights_of(&[
            (0, 1.0), (2, 1.0), (4, 1.0), (5, 1.0), (7, 1.0), (9, 1.0), (11, 1.0),
        ]);
        for tonic in [0u8, 2, 4, 5, 7, 9, 11] {
            let scale = identify_scale(&weights, tonic).unwrap();
            assert!(
                scale.scale.contains(&tonic),
                "{} is not in the scale named for it",
                PITCH_NAMES[tonic as usize]
            );
            assert_eq!(scale.scale[0], tonic, "the scale should read from its tonic");
        }
    }

    #[test]
    fn identify_scale_is_none_for_silence() {
        assert!(identify_scale(&[0.0; 12], 0).is_none());
    }

    #[test]
    fn sustained_notes_count_in_every_bar_they_cover() {
        // One 8-beat C-major chord across two 4/4 bars.
        let notes = vec![note(60, 0.0, 8.0), note(64, 0.0, 8.0), note(67, 0.0, 8.0)];
        let bars = chords_by_bar(&notes, 4.0, 16);
        assert_eq!(bars.len(), 2);
        assert_eq!(bars[0]["chord"], "Cmaj");
        assert_eq!(bars[1]["chord"], "Cmaj", "the pad still sounds in bar 2");
    }

    #[test]
    fn muted_notes_are_ignored() {
        let mut muted = note(66, 0.0, 4.0);
        muted.muted = true;
        let notes = vec![note(60, 0.0, 4.0), note(64, 0.0, 4.0), note(67, 0.0, 4.0), muted];
        assert_eq!(chords_by_bar(&notes, 4.0, 4)[0]["chord"], "Cmaj");
    }

    #[test]
    fn empty_bars_are_reported_as_silent() {
        let notes = vec![note(60, 8.0, 4.0)];
        let bars = chords_by_bar(&notes, 4.0, 16);
        assert_eq!(bars[0]["chord"], Value::Null);
        assert_eq!(bars[2]["chord"], "C (single note)");
    }
}
