//! Arrangement analysis: what plays when, and where the structure turns.
//!
//! Sections are inferred from *which tracks are active*, not from labels or
//! genre convention. A change in instrumentation is the one structural signal
//! that is actually in the data.

use serde_json::{Value, json};

use super::{Snapshot, TrackClip};

/// One inferred section of the arrangement.
#[derive(Debug)]
struct Section {
    start_bar: usize,
    end_bar: usize,
    active: Vec<usize>,
}

/// Which tracks have a clip sounding in each bar.
fn activity_grid(clips: &[TrackClip], beats_per_bar: f64, bars: usize) -> Vec<Vec<usize>> {
    (0..bars)
        .map(|bar| {
            let from = bar as f64 * beats_per_bar;
            let to = from + beats_per_bar;
            let mut active: Vec<usize> = clips
                .iter()
                .filter(|c| c.start < to - 1e-9 && c.end > from + 1e-9)
                .map(|c| c.track)
                .collect();
            active.sort_unstable();
            active.dedup();
            active
        })
        .collect()
}

/// Group consecutive bars that share the same set of active tracks.
fn sections(grid: &[Vec<usize>]) -> Vec<Section> {
    let mut sections: Vec<Section> = Vec::new();
    for (bar, active) in grid.iter().enumerate() {
        match sections.last_mut() {
            Some(last) if last.active == *active => last.end_bar = bar + 1,
            _ => sections.push(Section {
                start_bar: bar + 1,
                end_bar: bar + 1,
                active: active.clone(),
            }),
        }
    }
    sections
}

pub fn analyze(snapshot: &Snapshot, min_section_bars: usize) -> Value {
    let beats_per_bar = snapshot.beats_per_bar();
    let clips = snapshot.arrangement_clips();

    if clips.is_empty() {
        return json!({
            "arrangement_is_empty": true,
            "note": "No clips in the Arrangement timeline. Session clips are not analysed here \
                     — place them with duplicate_clip_to_arrangement first.",
            "tempo": snapshot.tempo,
            "beats_per_bar": beats_per_bar,
        });
    }

    let end_beat = clips.iter().map(|c| c.end).fold(0.0f64, f64::max);
    let total_bars = ((end_beat / beats_per_bar).ceil() as usize).max(1);
    let grid = activity_grid(&clips, beats_per_bar, total_bars);
    let raw_sections = sections(&grid);

    // Merge runs shorter than the threshold into their predecessor: a one-bar
    // gap where a clip happens to end early is not a section of the song.
    let mut merged: Vec<Section> = Vec::new();
    for section in raw_sections {
        let length = section.end_bar - section.start_bar + 1;
        match merged.last_mut() {
            Some(previous) if length < min_section_bars => previous.end_bar = section.end_bar,
            _ => merged.push(section),
        }
    }

    let name_of = |index: usize| -> String {
        snapshot
            .track_names
            .get(index)
            .cloned()
            .unwrap_or_else(|| format!("track {index}"))
    };

    let section_values: Vec<Value> = merged
        .iter()
        .map(|section| {
            json!({
                "bars": format!("{}-{}", section.start_bar, section.end_bar),
                "start_bar": section.start_bar,
                "length_bars": section.end_bar - section.start_bar + 1,
                "active_track_count": section.active.len(),
                "tracks": section.active.iter().map(|&i| name_of(i)).collect::<Vec<_>>(),
                "locator": snapshot.locator_at_bar(section.start_bar, beats_per_bar),
            })
        })
        .collect();

    // Where the instrumentation changes most sharply — the moments a listener
    // hears as a transition.
    let mut turns: Vec<Value> = Vec::new();
    for pair in merged.windows(2) {
        let (before, after) = (&pair[0], &pair[1]);
        let added: Vec<String> = after
            .active
            .iter()
            .filter(|t| !before.active.contains(t))
            .map(|&i| name_of(i))
            .collect();
        let removed: Vec<String> = before
            .active
            .iter()
            .filter(|t| !after.active.contains(t))
            .map(|&i| name_of(i))
            .collect();
        if added.is_empty() && removed.is_empty() {
            continue;
        }
        turns.push(json!({
            "at_bar": after.start_bar,
            "enters": added,
            "exits": removed,
            "track_count": format!("{} -> {}", before.active.len(), after.active.len()),
        }));
    }

    // Per-track span and coverage, which is what shows a part that enters once
    // and then never leaves.
    let per_track: Vec<Value> = (0..snapshot.track_names.len())
        .filter_map(|index| {
            let mine: Vec<&TrackClip> = clips.iter().filter(|c| c.track == index).collect();
            if mine.is_empty() {
                return None;
            }
            let first = mine.iter().map(|c| c.start).fold(f64::MAX, f64::min);
            let last = mine.iter().map(|c| c.end).fold(0.0f64, f64::max);
            let sounding: f64 = mine.iter().map(|c| c.end - c.start).sum();
            Some(json!({
                "track": name_of(index),
                "index": index,
                "clips": mine.len(),
                "enters_bar": (first / beats_per_bar).floor() as usize + 1,
                "leaves_bar": (last / beats_per_bar).ceil() as usize,
                "bars_sounding": (sounding / beats_per_bar).round() as usize,
                "coverage_percent": ((sounding / end_beat) * 100.0).round(),
            }))
        })
        .collect();

    let densities: Vec<usize> = grid.iter().map(Vec::len).collect();
    let busiest = densities.iter().enumerate().max_by_key(|&(_, n)| *n);
    let quietest = densities
        .iter()
        .enumerate()
        .filter(|&(_, n)| *n > 0)
        .min_by_key(|&(_, n)| *n);

    json!({
        "tempo": snapshot.tempo,
        "time_signature": snapshot.time_signature(),
        "total_bars": total_bars,
        "length_minutes": ((end_beat / snapshot.tempo.max(1.0)) * 100.0 / 60.0).round() / 100.0 * 60.0 / 60.0,
        "track_count": snapshot.track_names.len(),
        "sections": section_values,
        "transitions": turns,
        "tracks": per_track,
        "density": {
            "per_bar": densities,
            "busiest_bar": busiest.map(|(bar, n)| json!({ "bar": bar + 1, "tracks": n })),
            "sparsest_sounding_bar": quietest.map(|(bar, n)| json!({ "bar": bar + 1, "tracks": n })),
            "silent_bars": densities.iter().filter(|&&n| n == 0).count(),
        },
        "locators": snapshot.locators_with_bars(beats_per_bar),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn clip(track: usize, start: f64, end: f64) -> TrackClip {
        TrackClip {
            track,
            start,
            end,
            name: String::new(),
        }
    }

    fn snapshot(clips: Vec<TrackClip>, names: Vec<&str>) -> Snapshot {
        Snapshot {
            tempo: 120.0,
            signature_numerator: 4,
            signature_denominator: 4,
            track_names: names.into_iter().map(String::from).collect(),
            clips,
            notes: Vec::new(),
            mixer: Vec::new(),
            locators: Vec::new(),
        }
    }

    #[test]
    fn groups_bars_with_the_same_instrumentation_into_one_section() {
        // Pad throughout 8 bars; drums join for the last 4.
        let snap = snapshot(
            vec![clip(0, 0.0, 32.0), clip(1, 16.0, 32.0)],
            vec!["Pad", "Drums"],
        );
        let result = analyze(&snap, 1);
        let sections = result["sections"].as_array().unwrap();
        assert_eq!(sections.len(), 2);
        assert_eq!(sections[0]["bars"], "1-4");
        assert_eq!(sections[1]["bars"], "5-8");
        assert_eq!(sections[1]["tracks"], json!(["Pad", "Drums"]));
    }

    #[test]
    fn reports_what_enters_and_exits_at_a_transition() {
        let snap = snapshot(
            vec![clip(0, 0.0, 16.0), clip(1, 16.0, 32.0)],
            vec!["Pad", "Drums"],
        );
        let turns = analyze(&snap, 1)["transitions"].as_array().unwrap().clone();
        assert_eq!(turns.len(), 1);
        assert_eq!(turns[0]["at_bar"], 5);
        assert_eq!(turns[0]["enters"], json!(["Drums"]));
        assert_eq!(turns[0]["exits"], json!(["Pad"]));
    }

    #[test]
    fn short_runs_are_merged_rather_than_reported_as_sections() {
        // A one-bar hole in an otherwise continuous pad.
        let snap = snapshot(
            vec![clip(0, 0.0, 16.0), clip(0, 20.0, 32.0)],
            vec!["Pad"],
        );
        let loose = analyze(&snap, 1)["sections"].as_array().unwrap().len();
        let strict = analyze(&snap, 4)["sections"].as_array().unwrap().len();
        assert!(strict < loose, "a higher threshold should merge the hole away");
    }

    #[test]
    fn says_so_plainly_when_the_arrangement_is_empty() {
        let result = analyze(&snapshot(Vec::new(), vec!["Pad"]), 2);
        assert_eq!(result["arrangement_is_empty"], true);
        assert!(result["note"].as_str().unwrap().contains("duplicate_clip_to_arrangement"));
    }

    #[test]
    fn per_track_entry_and_coverage_are_reported() {
        let snap = snapshot(
            vec![clip(0, 0.0, 32.0), clip(1, 16.0, 32.0)],
            vec!["Pad", "Drums"],
        );
        let tracks = analyze(&snap, 1)["tracks"].as_array().unwrap().clone();
        let drums = tracks.iter().find(|t| t["track"] == "Drums").unwrap();
        assert_eq!(drums["enters_bar"], 5);
        assert_eq!(drums["bars_sounding"], 4);
        assert_eq!(drums["coverage_percent"], 50.0);
    }
}
