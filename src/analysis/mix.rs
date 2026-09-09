//! Mix analysis: register crowding and gain staging.
//!
//! All of this is arithmetic on what Live already reports. It cannot tell you
//! the mix sounds muddy, only that five parts are competing for the same two
//! octaves, which is the measurable half of that judgement.

use serde_json::{Value, json};

use super::{Note, Snapshot, PITCH_NAMES};

/// Live's fader is not linear in decibels; this is its published mapping at the
/// points that matter, interpolated in between.
fn fader_to_db(value: f64) -> f64 {
    // 0.85 is unity, 1.0 is +6 dB, and the taper steepens towards silence.
    const CURVE: &[(f64, f64)] = &[
        (0.0, -70.0),
        (0.2, -35.0),
        (0.4, -20.0),
        (0.6, -10.0),
        (0.7, -5.5),
        (0.85, 0.0),
        (1.0, 6.0),
    ];
    if value <= 0.0 {
        return f64::NEG_INFINITY;
    }
    for pair in CURVE.windows(2) {
        let (x0, y0) = pair[0];
        let (x1, y1) = pair[1];
        if value <= x1 {
            let t = (value - x0) / (x1 - x0);
            return y0 + t * (y1 - y0);
        }
    }
    6.0
}

/// MIDI note number to its approximate fundamental in hertz.
fn pitch_to_hz(pitch: u8) -> f64 {
    440.0 * 2f64.powf((pitch as f64 - 69.0) / 12.0)
}

fn note_name(pitch: u8) -> String {
    // MIDI 60 is C3 in Ableton's numbering, which is what the user sees.
    format!("{}{}", PITCH_NAMES[(pitch % 12) as usize], (pitch / 12) as i32 - 2)
}

struct Register {
    track: usize,
    name: String,
    low: u8,
    high: u8,
    /// Where the part's weight actually sits, duration-weighted.
    centre: f64,
    notes: usize,
}

fn registers(snapshot: &Snapshot) -> Vec<Register> {
    let mut out = Vec::new();
    for (index, name) in snapshot.track_names.iter().enumerate() {
        let mine: Vec<&Note> = snapshot
            .notes
            .iter()
            .filter(|n| n.track == index && !n.muted)
            .collect();
        if mine.is_empty() {
            continue;
        }
        let weight: f64 = mine.iter().map(|n| n.duration.max(0.01)).sum();
        let centre = mine
            .iter()
            .map(|n| n.pitch as f64 * n.duration.max(0.01))
            .sum::<f64>()
            / weight.max(1e-9);
        out.push(Register {
            track: index,
            name: name.clone(),
            low: mine.iter().map(|n| n.pitch).min().unwrap(),
            high: mine.iter().map(|n| n.pitch).max().unwrap(),
            centre,
            notes: mine.len(),
        });
    }
    out
}

pub fn analyze(snapshot: &Snapshot) -> Value {
    let regs = registers(snapshot);

    // Pairs of parts whose pitch ranges overlap substantially. Overlap is
    // measured against the narrower part, so a wide pad swallowing a bass
    // registers as a problem for the bass.
    let mut collisions: Vec<Value> = Vec::new();
    for (i, a) in regs.iter().enumerate() {
        for b in regs.iter().skip(i + 1) {
            let low = a.low.max(b.low) as f64;
            let high = (a.high.min(b.high)) as f64;
            if high < low {
                continue;
            }
            let shared = high - low + 1.0;
            let narrower = ((a.high - a.low) as f64).min((b.high - b.low) as f64) + 1.0;
            let ratio = shared / narrower.max(1.0);
            if ratio < 0.5 {
                continue;
            }
            collisions.push(json!({
                "tracks": [a.name.clone(), b.name.clone()],
                "shared_range": format!("{}-{}", note_name(low as u8), note_name(high as u8)),
                "overlap_percent": (ratio * 100.0).round().min(100.0),
                "centre_distance_semitones": (a.centre - b.centre).abs().round(),
            }));
        }
    }
    collisions.sort_by(|a, b| {
        b["overlap_percent"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&a["overlap_percent"].as_f64().unwrap_or(0.0))
    });

    let mixer: Vec<Value> = snapshot
        .mixer
        .iter()
        .map(|strip| {
            let db = fader_to_db(strip.volume);
            json!({
                "track": strip.name.clone(),
                "index": strip.track,
                "volume": (strip.volume * 1000.0).round() / 1000.0,
                "db": if db.is_finite() { json!((db * 10.0).round() / 10.0) } else { json!("-inf") },
                "panning": (strip.panning * 100.0).round() / 100.0,
                "muted": strip.muted,
                "soloed": strip.soloed,
                "active_sends": strip
                    .sends
                    .iter()
                    .enumerate()
                    .filter(|&(_, v)| *v > 0.001)
                    .map(|(i, v)| json!({ "send": i, "value": (v * 100.0).round() / 100.0 }))
                    .collect::<Vec<_>>(),
            })
        })
        .collect();

    // Everything panned dead centre is the default state, and worth noticing
    // when several parts share a register.
    let centred: Vec<String> = snapshot
        .mixer
        .iter()
        .filter(|s| s.panning.abs() < 0.02 && !s.muted)
        .map(|s| s.name.clone())
        .collect();

    let register_values: Vec<Value> = regs
        .iter()
        .map(|r| {
            json!({
                "track": r.name.clone(),
                "index": r.track,
                "range": format!("{}-{}", note_name(r.low), note_name(r.high)),
                "lowest_hz": pitch_to_hz(r.low).round(),
                "highest_hz": pitch_to_hz(r.high).round(),
                "centre_note": note_name(r.centre.round() as u8),
                "span_semitones": r.high - r.low,
                "notes": r.notes,
            })
        })
        .collect();

    json!({
        "registers": register_values,
        "register_collisions": collisions,
        "mixer": mixer,
        "observations": {
            "tracks_with_notes": regs.len(),
            "colliding_pairs": collisions.len(),
            "all_centred": centred,
            "muted_tracks": snapshot.mixer.iter().filter(|s| s.muted).map(|s| s.name.clone()).collect::<Vec<_>>(),
            "soloed_tracks": snapshot.mixer.iter().filter(|s| s.soloed).map(|s| s.name.clone()).collect::<Vec<_>>(),
        },
        "caveat": "Ranges are MIDI note fundamentals, not measured spectra: an instrument's \
                   harmonics and its devices both move where it actually sits. Treat collisions \
                   as places to listen, not as faults.",
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analysis::MixerStrip;

    fn note(track: usize, pitch: u8) -> Note {
        Note {
            pitch,
            start: 0.0,
            duration: 4.0,
            velocity: 100.0,
            muted: false,
            track,
            track_name: String::new(),
        }
    }

    fn snapshot(notes: Vec<Note>, names: Vec<&str>) -> Snapshot {
        Snapshot {
            tempo: 120.0,
            signature_numerator: 4,
            signature_denominator: 4,
            track_names: names.iter().map(|s| s.to_string()).collect(),
            clips: Vec::new(),
            notes,
            mixer: names
                .iter()
                .enumerate()
                .map(|(i, n)| MixerStrip {
                    track: i,
                    name: n.to_string(),
                    volume: 0.85,
                    panning: 0.0,
                    muted: false,
                    soloed: false,
                    sends: Vec::new(),
                })
                .collect(),
            locators: Vec::new(),
        }
    }

    #[test]
    fn fader_maps_to_the_decibels_live_shows() {
        assert!((fader_to_db(0.85) - 0.0).abs() < 0.01, "0.85 is unity");
        assert!((fader_to_db(1.0) - 6.0).abs() < 0.01, "1.0 is +6 dB");
        assert!(fader_to_db(0.0).is_infinite(), "0.0 is silence");
        assert!(fader_to_db(0.45) < -10.0 && fader_to_db(0.45) > -20.0);
    }

    #[test]
    fn flags_two_parts_sharing_a_register() {
        let notes = vec![note(0, 60), note(0, 67), note(1, 62), note(1, 65)];
        let result = analyze(&snapshot(notes, vec!["Pad A", "Pad B"]));
        let collisions = result["register_collisions"].as_array().unwrap();
        assert_eq!(collisions.len(), 1);
        assert_eq!(collisions[0]["overlap_percent"], 100.0);
    }

    #[test]
    fn ignores_parts_in_different_registers() {
        // A sub bass two octaves below a pad should not collide.
        let notes = vec![note(0, 36), note(0, 40), note(1, 72), note(1, 79)];
        let result = analyze(&snapshot(notes, vec!["Sub", "Pad"]));
        assert!(result["register_collisions"].as_array().unwrap().is_empty());
    }

    #[test]
    fn reports_the_duration_weighted_centre_not_the_midpoint() {
        // Mostly low notes with one brief high one: the centre should stay low.
        let mut notes = vec![note(0, 40), note(0, 41)];
        let mut brief = note(0, 90);
        brief.duration = 0.05;
        notes.push(brief);
        let result = analyze(&snapshot(notes, vec!["Bass"]));
        let centre = result["registers"][0]["centre_note"].as_str().unwrap();
        assert!(
            centre.starts_with("E1") || centre.starts_with("F1"),
            "centre drifted to {centre}"
        );
    }

    #[test]
    fn notes_use_abletons_octave_numbering() {
        assert_eq!(note_name(60), "C3", "MIDI 60 is C3 in Live");
        assert_eq!(note_name(36), "C1");
    }

    #[test]
    fn lists_tracks_left_at_dead_centre() {
        let result = analyze(&snapshot(vec![note(0, 60)], vec!["Pad A", "Pad B"]));
        assert_eq!(
            result["observations"]["all_centred"],
            json!(["Pad A", "Pad B"])
        );
    }
}
