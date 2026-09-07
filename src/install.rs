//! Installing the Remote Script into Ableton Live's User Library.
//!
//! Since Live 10.1.13, third-party control surfaces are loaded from
//! `<User Library>/Remote Scripts/`. The similarly named
//! `Preferences/User Remote Scripts/` is for legacy instant-mapping files and
//! is not scanned for Python scripts, so it is deliberately not a candidate.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};

/// Live names a Control Surface after its folder. Deliberately not
/// "AbletonMCP": the upstream project installs under that name, and this script
/// is designed to sit alongside it rather than replace it.
pub const SCRIPT_FOLDER: &str = "Crableton";

/// Where the script was written, and whether it replaced something.
pub struct Installed {
    pub path: PathBuf,
    pub replaced_version: Option<String>,
}

/// Write the bundled Remote Script into Live's User Library.
pub fn install(explicit_user_library: Option<&Path>) -> Result<Installed> {
    let user_library = match explicit_user_library {
        Some(path) => path.to_path_buf(),
        None => locate_user_library()
            .context("could not find Ableton's User Library; pass --user-library with its path")?,
    };

    if !user_library.is_dir() {
        bail!("{} is not a directory", user_library.display());
    }

    let dir = user_library.join("Remote Scripts").join(SCRIPT_FOLDER);
    let path = dir.join("__init__.py");

    let replaced_version = std::fs::read_to_string(&path).ok().and_then(|old| {
        installed_version(&old).map(str::to_string)
    });

    std::fs::create_dir_all(&dir)
        .with_context(|| format!("could not create {}", dir.display()))?;
    std::fs::write(&path, crate::REMOTE_SCRIPT)
        .with_context(|| format!("could not write {}", path.display()))?;

    Ok(Installed {
        path,
        replaced_version,
    })
}

/// Read `SCRIPT_VERSION = "..."` out of an installed script.
fn installed_version(source: &str) -> Option<&str> {
    source.lines().find_map(|line| {
        let rest = line.strip_prefix("SCRIPT_VERSION")?.trim_start();
        let rest = rest.strip_prefix('=')?.trim();
        rest.strip_prefix('"')?.split('"').next()
    })
}

/// Find the User Library, honouring a relocated one where we can read it.
pub fn locate_user_library() -> Option<PathBuf> {
    preference_dirs()
        .iter()
        .find_map(|dir| user_library_from_config(&dir.join("Library.cfg")))
        .filter(|path| path.is_dir())
        .or_else(|| default_user_library().filter(|path| path.is_dir()))
}

fn default_user_library() -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    let relative = if cfg!(target_os = "windows") {
        "Documents/Ableton/User Library"
    } else {
        "Music/Ableton/User Library"
    };
    Some(home.join(relative))
}

/// Ableton's preference folders (`.../Ableton/Live x.x.x`), newest first.
pub fn preference_dirs() -> Vec<PathBuf> {
    let Some(home) = dirs::home_dir() else {
        return Vec::new();
    };

    let base = if cfg!(target_os = "windows") {
        std::env::var_os("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData/Roaming"))
            .join("Ableton")
    } else if cfg!(target_os = "macos") {
        home.join("Library/Preferences/Ableton")
    } else {
        home.join(".config/ableton")
    };

    let Ok(entries) = std::fs::read_dir(&base) else {
        return Vec::new();
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.is_dir() && p.file_name().is_some_and(|n| n.to_string_lossy().starts_with("Live ")))
        .collect();

    // Newest Live first, comparing version numbers rather than strings so
    // "Live 12.10" sorts above "Live 12.9".
    dirs.sort_by_key(|p| std::cmp::Reverse(version_key(p)));
    dirs
}

fn version_key(path: &Path) -> Vec<u32> {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    name.split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse().ok())
        .collect()
}

/// Best-effort read of a relocated User Library path from `Library.cfg`.
///
/// Live stores it as a `ProjectPath` (the containing folder) plus a
/// `ProjectName`; the library is their join.
fn user_library_from_config(cfg: &Path) -> Option<PathBuf> {
    use quick_xml::events::Event;

    let source = std::fs::read_to_string(cfg).ok()?;
    let mut reader = quick_xml::Reader::from_str(&source);
    let mut buf = Vec::new();

    let mut in_user_library = false;
    let mut project_path: Option<String> = None;
    let mut project_name: Option<String> = None;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Eof) | Err(_) => break,
            Ok(Event::Start(e)) => {
                if e.name().as_ref() == b"UserLibrary" {
                    in_user_library = true;
                }
            }
            Ok(Event::End(e)) => {
                if e.name().as_ref() == b"UserLibrary" {
                    break;
                }
            }
            Ok(Event::Empty(e)) => {
                if !in_user_library {
                    buf.clear();
                    continue;
                }
                let tag = e.name();
                let slot = match tag.as_ref() {
                    b"ProjectPath" => &mut project_path,
                    b"ProjectName" => &mut project_name,
                    _ => {
                        buf.clear();
                        continue;
                    }
                };
                if let Some(Ok(attr)) = e.attributes().find(|a| {
                    a.as_ref().is_ok_and(|a| a.key.as_ref() == b"Value")
                }) {
                    *slot = String::from_utf8(attr.value.into_owned()).ok();
                }
            }
            _ => {}
        }
        buf.clear();
    }

    let path = PathBuf::from(project_path?);
    Some(match project_name {
        Some(name) if !name.is_empty() => path.join(name),
        _ => path,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_version_out_of_an_installed_script() {
        let source = "# header\nSCRIPT_VERSION = \"2.0.0\"\nPROTOCOL = 2\n";
        assert_eq!(installed_version(source), Some("2.0.0"));
        assert_eq!(installed_version("no version here"), None);
    }

    #[test]
    fn sorts_preference_dirs_by_version_not_string() {
        let mut dirs = [
            PathBuf::from("/x/Live 12.9.1"),
            PathBuf::from("/x/Live 12.10.0"),
            PathBuf::from("/x/Live 11.3.4"),
        ];
        dirs.sort_by_key(|p| std::cmp::Reverse(version_key(p)));
        assert_eq!(dirs[0], PathBuf::from("/x/Live 12.10.0"));
        assert_eq!(dirs[2], PathBuf::from("/x/Live 11.3.4"));
    }

    #[test]
    fn parses_a_relocated_user_library_from_library_cfg() {
        let dir = std::env::temp_dir().join("crableton-install-test");
        std::fs::create_dir_all(&dir).unwrap();
        let cfg = dir.join("Library.cfg");
        std::fs::write(
            &cfg,
            r#"<?xml version="1.0"?>
<Library>
  <UserLibrary>
    <LibraryProject>
      <ProjectPath Value="/Volumes/Audio/Ableton" />
      <ProjectName Value="User Library" />
    </LibraryProject>
  </UserLibrary>
</Library>"#,
        )
        .unwrap();

        assert_eq!(
            user_library_from_config(&cfg),
            Some(PathBuf::from("/Volumes/Audio/Ableton/User Library"))
        );
        std::fs::remove_dir_all(&dir).ok();
    }
}
