//! A bundled reference for Ableton's Live Object Model.
//!
//! The Remote Script can only reach what the Live API actually exposes, and the
//! shape of that API is not discoverable from the tools themselves. Compiling a
//! condensed reference in means a model can check whether something is even
//! possible — and interpret the fields it gets back — without network access
//! and without a Live version to interrogate.

use std::sync::OnceLock;

use serde::Deserialize;
use serde_json::{Value, json};

const LOM_JSON: &str = include_str!("../resources/lom.json");

#[derive(Debug, Deserialize)]
pub struct Reference {
    pub version: String,
    pub note: String,
    pub classes: Vec<Class>,
}

#[derive(Debug, Deserialize)]
pub struct Class {
    pub name: String,
    #[serde(default)]
    pub path: Option<String>,
    pub description: String,
    #[serde(default)]
    pub properties: Vec<Member>,
    #[serde(default)]
    pub methods: Vec<Member>,
    #[serde(default)]
    pub children: Vec<Member>,
}

#[derive(Debug, Deserialize)]
pub struct Member {
    pub name: String,
    #[serde(default)]
    pub r#type: Option<String>,
    #[serde(default)]
    pub access: Option<String>,
    #[serde(default)]
    pub description: String,
}

fn reference() -> &'static Reference {
    static REFERENCE: OnceLock<Reference> = OnceLock::new();
    REFERENCE.get_or_init(|| {
        serde_json::from_str(LOM_JSON).expect("bundled resources/lom.json is valid")
    })
}

/// Serve a `lom_reference` call entirely from the bundled data.
pub fn lookup(class_name: Option<&str>, query: Option<&str>) -> Value {
    let reference = reference();

    match (class_name, query) {
        (Some(name), _) => describe(reference, name),
        (None, Some(query)) => search(reference, query),
        (None, None) => index(reference),
    }
}

fn index(reference: &Reference) -> Value {
    json!({
        "version": reference.version,
        "note": reference.note,
        "usage": "Pass `class_name` for the full members of one class, or `query` to search.",
        "classes": reference
            .classes
            .iter()
            .map(|class| json!({
                "name": class.name,
                "path": class.path,
                "description": class.description,
                "members": class.properties.len() + class.methods.len() + class.children.len(),
            }))
            .collect::<Vec<_>>(),
    })
}

fn describe(reference: &Reference, name: &str) -> Value {
    let wanted = normalise(name);
    let found = reference
        .classes
        .iter()
        .find(|class| normalise(&class.name) == wanted)
        .or_else(|| {
            reference
                .classes
                .iter()
                .find(|class| normalise(&class.name).ends_with(&wanted))
        });

    let Some(class) = found else {
        return json!({
            "error": format!("no Live Object Model class named `{name}`"),
            "known_classes": reference.classes.iter().map(|c| &c.name).collect::<Vec<_>>(),
        });
    };

    json!({
        "name": class.name,
        "path": class.path,
        "description": class.description,
        "children": members(&class.children),
        "properties": members(&class.properties),
        "methods": members(&class.methods),
    })
}

fn members(list: &[Member]) -> Vec<Value> {
    list.iter()
        .map(|m| {
            let mut entry = serde_json::Map::new();
            entry.insert("name".into(), json!(m.name));
            if let Some(ty) = &m.r#type {
                entry.insert("type".into(), json!(ty));
            }
            if let Some(access) = &m.access {
                entry.insert("access".into(), json!(access));
            }
            if !m.description.is_empty() {
                entry.insert("description".into(), json!(m.description));
            }
            Value::Object(entry)
        })
        .collect()
}

fn search(reference: &Reference, query: &str) -> Value {
    let needle = query.trim().to_lowercase();
    if needle.is_empty() {
        return index(reference);
    }

    let mut hits = Vec::new();
    for class in &reference.classes {
        for (kind, list) in [
            ("property", &class.properties),
            ("method", &class.methods),
            ("child", &class.children),
        ] {
            for member in list {
                if member.name.to_lowercase().contains(&needle)
                    || member.description.to_lowercase().contains(&needle)
                {
                    hits.push(json!({
                        "class": class.name,
                        "kind": kind,
                        "name": member.name,
                        "type": member.r#type,
                        "access": member.access,
                        "description": member.description,
                    }));
                }
            }
        }
        if class.name.to_lowercase().contains(&needle)
            || class.description.to_lowercase().contains(&needle)
        {
            hits.push(json!({
                "class": class.name,
                "kind": "class",
                "name": class.name,
                "description": class.description,
            }));
        }
    }

    // Keep the payload bounded: a broad query like "value" would otherwise
    // return most of the model.
    let total = hits.len();
    hits.truncate(60);
    json!({
        "query": query,
        "total_matches": total,
        "returned": hits.len(),
        "matches": hits,
    })
}

/// `Song.View`, `song_view` and `songview` should all find the same class.
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
        assert!(reference.classes.len() >= 15, "reference looks truncated");
        for class in &reference.classes {
            assert!(!class.description.is_empty(), "{} has no description", class.name);
            assert!(
                !(class.properties.is_empty() && class.methods.is_empty() && class.children.is_empty()),
                "{} has no members",
                class.name
            );
        }
    }

    #[test]
    fn looks_up_classes_case_and_punctuation_insensitively() {
        for name in ["Song", "song", "Song.View", "song_view"] {
            let found = lookup(Some(name), None);
            assert!(found.get("error").is_none(), "{name} was not found");
        }
        assert!(lookup(Some("NotAClass"), None).get("error").is_some());
    }

    #[test]
    fn searches_members_and_bounds_the_result() {
        let hits = lookup(None, Some("warp"));
        assert!(hits["total_matches"].as_u64().unwrap() > 0);
        assert!(hits["matches"].as_array().unwrap().len() <= 60);
    }

    #[test]
    fn no_arguments_returns_the_class_index() {
        let index = lookup(None, None);
        assert!(index["classes"].as_array().unwrap().len() >= 15);
    }
}
