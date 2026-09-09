//! A bundled reference for Live's stock devices.
//!
//! `get_device_parameters` reports that Roar has forty parameters with names.
//! It cannot say that three of them matter, that `Filter Morph` is inert
//! outside Morph mode, or that driving a pad crowds everything above it. That
//! is what this carries, keyed by `class_name` so the two join up.

use std::sync::OnceLock;

use serde::Deserialize;
use serde_json::{Value, json};

const DEVICES_JSON: &str = include_str!("../resources/devices.json");

#[derive(Debug, Deserialize)]
struct Reference {
    note: String,
    reading_values: String,
    devices: Vec<Device>,
}

#[derive(Debug, Deserialize)]
struct Device {
    class_name: String,
    name: String,
    kind: String,
    summary: String,
    #[serde(default)]
    key_parameters: Vec<Parameter>,
    #[serde(default)]
    gotchas: Option<String>,
}

#[derive(Debug, Deserialize)]
struct Parameter {
    name: String,
    does: String,
    #[serde(default)]
    quantized: Option<bool>,
    #[serde(default)]
    automate: Option<String>,
}

fn reference() -> &'static Reference {
    static REFERENCE: OnceLock<Reference> = OnceLock::new();
    REFERENCE.get_or_init(|| {
        serde_json::from_str(DEVICES_JSON).expect("bundled resources/devices.json is valid")
    })
}

/// Serve a `device_reference` call from the bundled data.
pub fn lookup(device: Option<&str>, query: Option<&str>) -> Value {
    let reference = reference();
    match (device, query) {
        (Some(name), _) => describe(reference, name),
        (None, Some(query)) => search(reference, query),
        (None, None) => index(reference),
    }
}

fn index(reference: &Reference) -> Value {
    json!({
        "note": reference.note,
        "reading_values": reference.reading_values,
        "usage": "Pass `device` with a class_name or display name for the full entry, or \
                  `query` to search across summaries and parameters.",
        "devices": reference
            .devices
            .iter()
            .map(|d| json!({
                "class_name": d.class_name,
                "name": d.name,
                "kind": d.kind,
                "summary": d.summary,
            }))
            .collect::<Vec<_>>(),
    })
}

/// Match on `class_name` first, since that is what Live reports, then on the
/// display name, so both "InstrumentVector" and "Wavetable" resolve.
fn describe(reference: &Reference, wanted: &str) -> Value {
    let needle = normalise(wanted);
    let found = reference
        .devices
        .iter()
        .find(|d| normalise(&d.class_name) == needle)
        .or_else(|| reference.devices.iter().find(|d| normalise(&d.name) == needle))
        .or_else(|| {
            reference
                .devices
                .iter()
                .find(|d| normalise(&d.name).contains(&needle) || needle.contains(&normalise(&d.name)))
        });

    let Some(device) = found else {
        return json!({
            "error": format!("no bundled reference for `{wanted}`"),
            "note": "Only Live's most-used stock devices are covered. For anything else, \
                     get_device_parameters still reports names, ranges, display values and \
                     the named settings of quantized parameters.",
            "known": reference.devices.iter().map(|d| &d.class_name).collect::<Vec<_>>(),
        });
    };

    json!({
        "class_name": device.class_name,
        "name": device.name,
        "kind": device.kind,
        "summary": device.summary,
        "key_parameters": device.key_parameters.iter().map(parameter).collect::<Vec<_>>(),
        "gotchas": device.gotchas,
        "reading_values": reference.reading_values,
    })
}

fn parameter(p: &Parameter) -> Value {
    let mut entry = serde_json::Map::new();
    entry.insert("name".into(), json!(p.name));
    entry.insert("does".into(), json!(p.does));
    if p.quantized == Some(true) {
        entry.insert("quantized".into(), json!(true));
    }
    if let Some(automate) = &p.automate {
        entry.insert("worth_automating".into(), json!(automate));
    }
    Value::Object(entry)
}

fn search(reference: &Reference, query: &str) -> Value {
    let needle = query.trim().to_lowercase();
    if needle.is_empty() {
        return index(reference);
    }

    let mut hits = Vec::new();
    for device in &reference.devices {
        if device.name.to_lowercase().contains(&needle)
            || device.class_name.to_lowercase().contains(&needle)
            || device.summary.to_lowercase().contains(&needle)
        {
            hits.push(json!({
                "class_name": device.class_name,
                "name": device.name,
                "kind": device.kind,
                "summary": device.summary,
                "matched": "device",
            }));
            continue;
        }
        let matching: Vec<Value> = device
            .key_parameters
            .iter()
            .filter(|p| {
                p.name.to_lowercase().contains(&needle) || p.does.to_lowercase().contains(&needle)
            })
            .map(parameter)
            .collect();
        if !matching.is_empty() {
            hits.push(json!({
                "class_name": device.class_name,
                "name": device.name,
                "matched": "parameters",
                "parameters": matching,
            }));
        }
    }

    json!({ "query": query, "matches": hits.len(), "results": hits })
}

fn normalise(name: &str) -> String {
    name.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundled_reference_parses_and_is_populated() {
        let reference = reference();
        assert!(reference.devices.len() >= 10);
        for device in &reference.devices {
            assert!(!device.summary.is_empty(), "{} has no summary", device.class_name);
            assert!(
                !device.key_parameters.is_empty(),
                "{} lists no parameters",
                device.class_name
            );
            for p in &device.key_parameters {
                assert!(!p.does.is_empty(), "{}.{} explains nothing", device.class_name, p.name);
            }
        }
    }

    #[test]
    fn resolves_by_class_name_and_by_display_name() {
        // Live reports "InstrumentVector"; a person says "Wavetable".
        for name in ["InstrumentVector", "Wavetable", "wavetable"] {
            let found = lookup(Some(name), None);
            assert_eq!(found["class_name"], "InstrumentVector", "failed for {name}");
        }
        assert_eq!(lookup(Some("Roar"), None)["name"], "Roar");
        assert_eq!(lookup(Some("Eq8"), None)["name"], "EQ Eight");
    }

    #[test]
    fn unknown_devices_explain_the_fallback() {
        let missing = lookup(Some("SomeVstPlugin"), None);
        assert!(missing["error"].is_string());
        assert!(
            missing["note"].as_str().unwrap().contains("get_device_parameters"),
            "should point at the tool that still works"
        );
    }

    #[test]
    fn searches_parameters_as_well_as_devices() {
        let hits = lookup(None, Some("cutoff"));
        assert!(hits["matches"].as_u64().unwrap() > 0);

        let by_purpose = lookup(None, Some("reverb"));
        assert!(by_purpose["matches"].as_u64().unwrap() > 0);
    }

    #[test]
    fn the_index_lists_everything() {
        let index = lookup(None, None);
        assert_eq!(
            index["devices"].as_array().unwrap().len(),
            reference().devices.len()
        );
    }
}
