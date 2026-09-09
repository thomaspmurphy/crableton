//! Musical analysis computed from a single session snapshot.
//!
//! The division of labour here is deliberate: these tools report *facts*, //! what the notes are, when parts enter, which registers overlap, and leave
//! the judgement to the caller. Taste does not belong in a server.
//!
//! It all runs off one `get_session_snapshot` call, so the large payload stays
//! on this side and only the analysis reaches the model.

pub mod arrangement;
pub mod harmony;
pub mod mix;

use serde_json::{Value, json};

/// Sharps throughout: Live's own note names, and picking spellings per key
/// would imply a harmonic reading the analysis has not earned.
pub const PITCH_NAMES: [&str; 12] = [
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
];

#[derive(Debug, Clone)]
pub struct Note {
    pub pitch: u8,
    pub start: f64,
    pub duration: f64,
    pub velocity: f64,
    pub muted: bool,
    pub track: usize,
    pub track_name: String,
}

#[derive(Debug, Clone)]
pub struct TrackClip {
    pub track: usize,
    pub start: f64,
    pub end: f64,
    pub name: String,
}

#[derive(Debug, Clone)]
pub struct MixerStrip {
    pub track: usize,
    pub name: String,
    pub volume: f64,
    pub panning: f64,
    pub muted: bool,
    pub soloed: bool,
    pub sends: Vec<f64>,
}

/// The parts of a session snapshot the analyses need.
#[derive(Debug, Clone)]
pub struct Snapshot {
    pub tempo: f64,
    pub signature_numerator: u32,
    pub signature_denominator: u32,
    pub track_names: Vec<String>,
    pub clips: Vec<TrackClip>,
    pub notes: Vec<Note>,
    pub mixer: Vec<MixerStrip>,
    pub locators: Vec<(String, f64)>,
}

impl Snapshot {
    pub fn beats_per_bar(&self) -> f64 {
        // A bar is `numerator` beats of `1/denominator`, expressed in Live's
        // quarter-note beats: 6/8 is three quarter-notes, not six.
        let numerator = self.signature_numerator.max(1) as f64;
        let denominator = self.signature_denominator.max(1) as f64;
        numerator * (4.0 / denominator)
    }

    pub fn time_signature(&self) -> String {
        format!("{}/{}", self.signature_numerator, self.signature_denominator)
    }

    pub fn arrangement_clips(&self) -> Vec<TrackClip> {
        self.clips.clone()
    }

    pub fn locator_at_bar(&self, bar: usize, beats_per_bar: f64) -> Option<String> {
        self.locators
            .iter()
            .find(|(_, beat)| {
                let locator_bar = (beat / beats_per_bar).floor() as usize + 1;
                locator_bar == bar
            })
            .map(|(name, _)| name.clone())
    }

    pub fn locators_with_bars(&self, beats_per_bar: f64) -> Vec<Value> {
        self.locators
            .iter()
            .map(|(name, beat)| {
                json!({
                    "name": name,
                    "beat": beat,
                    "bar": (beat / beats_per_bar).floor() as usize + 1,
                })
            })
            .collect()
    }

    /// Build a snapshot from what the Remote Script returns.
    ///
    /// Tolerant by design: a field the installed script does not send should
    /// weaken the analysis, not fail it.
    pub fn from_value(raw: &Value) -> Self {
        let session = raw.get("session").unwrap_or(raw);
        let tempo = session
            .get("tempo")
            .and_then(Value::as_f64)
            .unwrap_or(120.0);
        let signature_numerator = session
            .get("signature_numerator")
            .and_then(Value::as_u64)
            .unwrap_or(4) as u32;
        let signature_denominator = session
            .get("signature_denominator")
            .and_then(Value::as_u64)
            .unwrap_or(4) as u32;

        let empty = Vec::new();
        let tracks = raw
            .get("tracks")
            .and_then(Value::as_array)
            .unwrap_or(&empty);

        let mut track_names = Vec::with_capacity(tracks.len());
        let mut clips = Vec::new();
        let mut notes = Vec::new();
        let mut mixer = Vec::new();

        for (index, track) in tracks.iter().enumerate() {
            let name = track
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("unnamed")
                .to_string();
            track_names.push(name.clone());

            mixer.push(MixerStrip {
                track: index,
                name: name.clone(),
                volume: track.get("volume").and_then(Value::as_f64).unwrap_or(0.85),
                panning: track.get("panning").and_then(Value::as_f64).unwrap_or(0.0),
                muted: track.get("mute").and_then(Value::as_bool).unwrap_or(false),
                soloed: track.get("solo").and_then(Value::as_bool).unwrap_or(false),
                sends: track
                    .get("sends")
                    .and_then(Value::as_array)
                    .map(|sends| {
                        sends
                            .iter()
                            .filter_map(|s| s.get("value").and_then(Value::as_f64))
                            .collect()
                    })
                    .unwrap_or_default(),
            });

            for clip in track
                .get("arrangement_clips")
                .and_then(Value::as_array)
                .unwrap_or(&empty)
            {
                let start = clip.get("start_time").and_then(Value::as_f64);
                let end = clip.get("end_time").and_then(Value::as_f64);
                if let (Some(start), Some(end)) = (start, end)
                    && end > start
                {
                    clips.push(TrackClip {
                        track: index,
                        start,
                        end,
                        name: clip
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                    });
                }
            }

            // Notes come from the Session clips, which is where they are
            // authored; Arrangement copies share the same material.
            for clip in track
                .get("session_clips")
                .and_then(Value::as_array)
                .unwrap_or(&empty)
            {
                for note in clip.get("notes").and_then(Value::as_array).unwrap_or(&empty) {
                    let pitch = note.get("pitch").and_then(Value::as_u64);
                    let start = note.get("start_time").and_then(Value::as_f64);
                    let duration = note.get("duration").and_then(Value::as_f64);
                    if let (Some(pitch), Some(start), Some(duration)) = (pitch, start, duration)
                        && pitch <= 127
                    {
                        notes.push(Note {
                            pitch: pitch as u8,
                            start,
                            duration,
                            velocity: note
                                .get("velocity")
                                .and_then(Value::as_f64)
                                .unwrap_or(100.0),
                            muted: note.get("mute").and_then(Value::as_bool).unwrap_or(false),
                            track: index,
                            track_name: name.clone(),
                        });
                    }
                }
            }
        }

        let locators = raw
            .get("locators")
            .and_then(Value::as_array)
            .map(|list| {
                list.iter()
                    .filter_map(|l| {
                        Some((
                            l.get("name").and_then(Value::as_str).unwrap_or("").to_string(),
                            l.get("time").and_then(Value::as_f64)?,
                        ))
                    })
                    .collect()
            })
            .unwrap_or_default();

        Self {
            tempo,
            signature_numerator,
            signature_denominator,
            track_names,
            clips,
            notes,
            mixer,
            locators,
        }
    }
}

/// Harmonic analysis, optionally narrowed to one track.
pub fn analyze_harmony(snapshot: &Snapshot, track: Option<usize>, max_bars: usize) -> Value {
    let notes: Vec<Note> = match track {
        Some(index) => snapshot
            .notes
            .iter()
            .filter(|n| n.track == index)
            .cloned()
            .collect(),
        None => snapshot.notes.clone(),
    };

    if notes.is_empty() {
        return json!({
            "no_notes": true,
            "note": match track {
                Some(i) => format!(
                    "Track {i} has no MIDI notes in its Session clips. Audio tracks and empty \
                     tracks cannot be analysed harmonically."
                ),
                None => "No MIDI notes found in any Session clip.".to_string(),
            },
        });
    }

    let weights = harmony::pitch_class_weights(&notes);
    let key = harmony::estimate_key(&weights);
    let beats_per_bar = snapshot.beats_per_bar();

    let total: f64 = weights.iter().sum();
    let distribution: Vec<Value> = (0..12)
        .filter(|&i| weights[i] > 0.0)
        .map(|i| {
            json!({
                "pitch": PITCH_NAMES[i],
                "weight_percent": ((weights[i] / total) * 1000.0).round() / 10.0,
            })
        })
        .collect();

    let mut result = json!({
        "scope": match track {
            Some(i) => json!({ "track": snapshot.track_names.get(i), "index": i }),
            None => json!("whole set"),
        },
        "note_count": notes.len(),
        "pitch_distribution": distribution,
        "chords_by_bar": harmony::chords_by_bar(&notes, beats_per_bar, max_bars),
    });

    if let Some(key) = key {
        let weight_of = |pc: u8| ((weights[pc as usize] / total) * 1000.0).round() / 10.0;

        // The correlation locates the tonic reliably but can only ever answer
        // "major" or "minor". The mode comes from the notes themselves, so
        // that Dorian is named Dorian instead of a minor key with a wrong
        // sixth, and the scale reported is one the music actually uses.
        let scale = harmony::identify_scale(&weights, key.tonic);

        result["key"] = match &scale {
            Some(scale) => {
                let chromatic: Vec<Value> = scale
                    .chromatic
                    .iter()
                    .map(|&pc| json!({ "pitch": PITCH_NAMES[pc as usize], "weight_percent": weight_of(pc) }))
                    .collect();

                let mut key = json!({
                    "estimate": scale.name(),
                    "tonic": PITCH_NAMES[scale.tonic as usize],
                    "mode": scale.mode,
                    "collection": scale.collection,
                    "scale_pitches": scale.scale.iter().map(|&pc| PITCH_NAMES[pc as usize]).collect::<Vec<_>>(),
                    "notes_outside_the_scale": chromatic,
                    "tonic_confidence": (key.correlation * 100.0).round() / 100.0,
                    "tonic_margin_over_runner_up": (key.margin * 100.0).round() / 100.0,
                    "tonic_ambiguous": key.margin < 0.05,
                    // Distinct from the tonic being uncertain: the notes played
                    // may simply not pin down which rotation this is.
                    "mode_determined_by_the_notes": scale.exact,
                });
                if !scale.alternatives.is_empty() {
                    key["equally_consistent_readings"] = json!(scale.alternatives);
                }
                if !scale.exact && scale.chromatic.is_empty() {
                    key["note"] = json!(
                        "Not every degree of the collection is played, so the mode is inferred \
                         rather than pinned down. The alternatives listed fit the notes just \
                         as well."
                    );
                }
                key
            }
            // No pitch content at all; fall back to reporting the correlation.
            None => json!({
                "estimate": key.name(),
                "tonic_confidence": (key.correlation * 100.0).round() / 100.0,
                "scale_pitches": key.scale().iter().map(|&pc| PITCH_NAMES[pc as usize]).collect::<Vec<_>>(),
            }),
        };
    }

    result["caveat"] = json!(
        "Key and chord names are inferred from pitch content alone, with no voicing or \
         register and no rhythmic weight beyond note length. The tonic comes from profile \
         correlation and the mode from the played pitch classes, so heavily chromatic \
         material, or a passage using only part of its scale, will read approximately. \
         Check `mode_determined_by_the_notes` before relying on the mode."
    );
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_snapshot_from_the_remote_script_shape() {
        let raw = json!({
            "session": { "tempo": 124.0, "signature_numerator": 4, "signature_denominator": 4 },
            "tracks": [{
                "name": "Pad",
                "volume": 0.45,
                "panning": -0.2,
                "mute": false,
                "solo": false,
                "sends": [{ "index": 0, "value": 0.3 }],
                "arrangement_clips": [{ "name": "a", "start_time": 128.0, "end_time": 256.0 }],
                "session_clips": [{
                    "notes": [
                        { "pitch": 60, "start_time": 0.0, "duration": 4.0, "velocity": 100 },
                        { "pitch": 64, "start_time": 0.0, "duration": 4.0, "velocity": 90 }
                    ]
                }]
            }],
            "locators": [{ "name": "Verse", "time": 64.0 }]
        });

        let snapshot = Snapshot::from_value(&raw);
        assert_eq!(snapshot.tempo, 124.0);
        assert_eq!(snapshot.track_names, vec!["Pad"]);
        assert_eq!(snapshot.notes.len(), 2);
        assert_eq!(snapshot.clips.len(), 1);
        assert_eq!(snapshot.mixer[0].volume, 0.45);
        assert_eq!(snapshot.mixer[0].sends, vec![0.3]);
        assert_eq!(snapshot.locators, vec![("Verse".to_string(), 64.0)]);
    }

    #[test]
    fn missing_fields_weaken_the_analysis_rather_than_breaking_it() {
        let snapshot = Snapshot::from_value(&json!({ "tracks": [{ "name": "X" }] }));
        assert_eq!(snapshot.tempo, 120.0);
        assert_eq!(snapshot.beats_per_bar(), 4.0);
        assert!(snapshot.notes.is_empty());
        assert!(snapshot.clips.is_empty());
        assert_eq!(snapshot.mixer.len(), 1);
    }

    #[test]
    fn beats_per_bar_accounts_for_the_denominator() {
        let mut snapshot = Snapshot::from_value(&json!({}));
        snapshot.signature_numerator = 6;
        snapshot.signature_denominator = 8;
        assert_eq!(snapshot.beats_per_bar(), 3.0, "6/8 is three quarter-notes");
        snapshot.signature_numerator = 7;
        snapshot.signature_denominator = 4;
        assert_eq!(snapshot.beats_per_bar(), 7.0);
    }

    #[test]
    fn harmony_reports_no_notes_rather_than_guessing() {
        let snapshot = Snapshot::from_value(&json!({ "tracks": [{ "name": "Audio" }] }));
        let result = analyze_harmony(&snapshot, None, 32);
        assert_eq!(result["no_notes"], true);
    }

    #[test]
    fn locators_are_reported_in_bars() {
        let raw = json!({ "locators": [{ "name": "Drop", "time": 64.0 }] });
        let snapshot = Snapshot::from_value(&raw);
        let listed = snapshot.locators_with_bars(4.0);
        assert_eq!(listed[0]["bar"], 17, "beat 64 is bar 17 in 4/4");
    }

    /// A clip of `(pitch, start, duration)` notes, as the Remote Script sends them.
    fn set_of(notes: &[(u8, f64, f64)]) -> Snapshot {
        let notes: Vec<Value> = notes
            .iter()
            .map(|&(pitch, start, duration)| {
                json!({ "pitch": pitch, "start_time": start, "duration": duration })
            })
            .collect();
        Snapshot::from_value(&json!({
            "tracks": [{ "name": "Lead", "session_clips": [{ "notes": notes }] }]
        }))
    }

    #[test]
    fn genuinely_chromatic_notes_are_surfaced() {
        // A full C major scale with a passing F#. No diatonic rotation can
        // hold C, D, E, F, G, A, B and F# at once, so the F# is chromatic.
        let mut notes: Vec<(u8, f64, f64)> = [60u8, 62, 64, 65, 67, 69, 71]
            .iter()
            .enumerate()
            .map(|(i, &p)| (p, i as f64 * 4.0, 4.0))
            .collect();
        notes.push((66, 28.0, 0.5));

        let result = analyze_harmony(&set_of(&notes), None, 16);
        let outside = result["key"]["notes_outside_the_scale"].as_array().unwrap();
        assert_eq!(outside.len(), 1, "only the F# is chromatic here");
        assert_eq!(outside[0]["pitch"], "F#");
    }

    #[test]
    fn a_raised_fourth_reads_as_lydian_rather_than_a_wrong_note() {
        // The whole point of the mode detection: an F# over a C tonic is the
        // Lydian fourth when the rest of the material supports it, not an
        // outlier against an assumed C major.
        //
        // The tonic has to be audible in the weighting for this to work. The
        // same seven notes played evenly are just the G-major collection with
        // no tonal centre, and the correlation will rightly say so, which is
        // why real Lydian writing leans on its tonic.
        let mut notes: Vec<(u8, f64, f64)> = [60u8, 62, 64, 66, 67, 69, 71]
            .iter()
            .enumerate()
            .map(|(i, &p)| (p, i as f64 * 4.0, 4.0))
            .collect();
        for bar in 0..4 {
            notes.push((60, 28.0 + bar as f64 * 8.0, 8.0));
        }

        let key = &analyze_harmony(&set_of(&notes), None, 16)["key"];
        assert_eq!(key["estimate"], "C Lydian");
        assert_eq!(key["mode"], "Lydian");
        assert_eq!(key["mode_determined_by_the_notes"], true);
        assert!(
            key["notes_outside_the_scale"].as_array().unwrap().is_empty(),
            "every note belongs to the mode"
        );
        assert_eq!(key["scale_pitches"][3], "F#", "the fourth degree is raised");
    }

    #[test]
    fn an_underdetermined_mode_says_so_and_offers_the_alternatives() {
        // A bare triad cannot pin down a seven-note mode.
        let result = analyze_harmony(&set_of(&[(60, 0.0, 4.0), (64, 0.0, 4.0), (67, 0.0, 4.0)]), None, 8);
        let key = &result["key"];
        assert_eq!(key["mode_determined_by_the_notes"], false);
        assert!(key["equally_consistent_readings"].as_array().is_some_and(|a| !a.is_empty()));
        assert!(key["note"].as_str().unwrap().contains("inferred"));
    }
}
