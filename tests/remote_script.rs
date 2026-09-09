//! The Rust tool table and the Python Remote Script are two halves of one
//! protocol, and nothing at compile time ties them together. These tests do:
//! a command added on one side and forgotten on the other fails here rather
//! than at the first tool call against a live set.

use crableton::{REMOTE_SCRIPT, REMOTE_SCRIPT_VERSION, tools};
use std::collections::HashSet;

/// Commands the Remote Script routes before the handler table.
const SPECIAL_COMMANDS: &[&str] = &["batch", "get_script_info"];

/// Commands crableton answers itself. Either it never touches Live (the
/// bundled references) or it gathers its own snapshot (the analyses), so
/// neither needs a handler in the Remote Script.
fn is_local(command: &str) -> bool {
    command.starts_with("__local_")
}

/// Handler names registered in `_build_handlers`.
///
/// Scoped to that method: elsewhere the script is full of dict literals like
/// `"live_version": self._live_version(),` that look identical line by line.
fn registered_handlers() -> HashSet<String> {
    let body = REMOTE_SCRIPT
        .split_once("def _build_handlers(self):")
        .expect("the script defines _build_handlers")
        .1;
    // The table is one `return { ... }`; stop at the line that closes it.
    let body = body
        .split_once("\n        }")
        .expect("_build_handlers returns a dict literal")
        .0;

    let mut found = HashSet::new();
    for line in body.lines() {
        let line = line.trim();
        let Some((name, tail)) = line.strip_prefix('"').and_then(|r| r.split_once('"')) else {
            continue;
        };
        let Some(target) = tail.trim().strip_prefix(": self.") else {
            continue;
        };
        // A bare method reference, not a call: `self._set_tempo,`.
        let target = target.trim_end_matches(',');
        if target.starts_with('_') && !target.contains('(') {
            found.insert(name.to_string());
        }
    }
    assert!(
        found.len() > 50,
        "only parsed {} handlers, the table's shape has changed",
        found.len()
    );
    found
}

/// Method names the script actually defines.
fn defined_methods() -> HashSet<String> {
    REMOTE_SCRIPT
        .lines()
        .filter_map(|line| {
            let rest = line.trim().strip_prefix("def ")?;
            let (name, _) = rest.split_once('(')?;
            Some(name.to_string())
        })
        .collect()
}

#[test]
fn every_tool_command_has_a_remote_handler() {
    let handlers = registered_handlers();
    let missing: Vec<_> = tools::all()
        .iter()
        .filter(|tool| !is_local(tool.command))
        .filter(|tool| {
            !handlers.contains(tool.command) && !SPECIAL_COMMANDS.contains(&tool.command)
        })
        .map(|tool| format!("{} -> {}", tool.name, tool.command))
        .collect();

    assert!(
        missing.is_empty(),
        "these tools forward to commands the Remote Script does not handle: {missing:#?}"
    );
}

#[test]
fn every_registered_handler_is_implemented() {
    let methods = defined_methods();
    let unimplemented: Vec<_> = registered_handlers()
        .into_iter()
        .filter(|name| !methods.contains(&format!("_{name}")))
        .collect();

    assert!(
        unimplemented.is_empty(),
        "the handler table names methods that do not exist: {unimplemented:#?}"
    );
}

#[test]
fn every_remote_handler_is_reachable_from_a_tool() {
    let used: HashSet<&str> = tools::all().iter().map(|tool| tool.command).collect();
    let orphaned: Vec<_> = registered_handlers()
        .into_iter()
        .filter(|name| !used.contains(name.as_str()))
        .collect();

    assert!(
        orphaned.is_empty(),
        "the Remote Script handles commands no tool can reach: {orphaned:#?}"
    );
}

#[test]
fn bundled_script_version_matches_the_constant() {
    let declared = REMOTE_SCRIPT
        .lines()
        .find_map(|line| {
            let rest = line.strip_prefix("SCRIPT_VERSION")?.trim_start();
            rest.strip_prefix('=')?.trim().strip_prefix('"')?.split('"').next()
        })
        .expect("the Remote Script declares SCRIPT_VERSION");

    assert_eq!(
        declared, REMOTE_SCRIPT_VERSION,
        "REMOTE_SCRIPT_VERSION and the script's own SCRIPT_VERSION have drifted"
    );
}

#[test]
fn handlers_all_take_the_deferred_response_argument() {
    // Every handler is called as `handler(params, respond)`; one written with
    // the old two-argument shape would fail only when that command is used.
    let bad: Vec<_> = REMOTE_SCRIPT
        .lines()
        .map(str::trim)
        .filter(|line| line.starts_with("def _"))
        .filter(|line| {
            let Some((name, _)) = line.trim_start_matches("def ").split_once('(') else {
                return false;
            };
            registered_handlers().contains(name.trim_start_matches('_'))
                && !line.contains("(self, params, respond)")
        })
        .map(str::to_string)
        .collect();

    assert!(bad.is_empty(), "handlers with the wrong signature: {bad:#?}");
}
