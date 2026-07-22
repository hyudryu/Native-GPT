//! Built-in (factory-default) tool sources embedded in the binary at build
//! time. A tool is rollback-eligible iff its `<id>/` folder exists in this
//! bundle. Embedding (vs. a runtime DB snapshot) means rollback always
//! restores exactly what shipped with the running binary, immune to edits.

use std::path::Path;

use include_dir::{include_dir, Dir};

/// The shipped `tools/` tree, captured at compile time. Only folders with
/// both `manifest.json` and `tool.py` are considered built-in tools.
static BUNDLED_TOOLS: Dir<'static> = include_dir!("$CARGO_MANIFEST_DIR/../../tools");

/// True if `<id>` is a built-in tool (shipped with the app).
pub fn is_bundled(id: &str) -> bool {
    has_bundled_file(id, "manifest.json") && has_bundled_file(id, "tool.py")
}

/// True if `<id>/<file>` exists in the embedded bundle.
fn has_bundled_file(id: &str, file: &str) -> bool {
    BUNDLED_TOOLS
        .get_dir(id)
        .is_some_and(|dir| dir.entries().into_iter().any(|entry| entry.path().file_name().is_some_and(|name| name == file)))
}

/// Restore a built-in tool's `manifest.json` + `tool.py` to the shipped
/// version. Returns Ok only if the id is bundled and writes succeed.
pub fn restore(repo_root: &Path, id: &str) -> Result<(), String> {
    let dir = BUNDLED_TOOLS
        .get_dir(id)
        .ok_or_else(|| format!("tool {id} is not a built-in tool"))?;
    let dest = repo_root.join("tools").join(id);
    std::fs::create_dir_all(&dest).map_err(|e| e.to_string())?;
    for file in ["manifest.json", "tool.py"] {
        let entry = dir
            .entries()
            .into_iter()
            .find(|entry| entry.path().file_name().is_some_and(|name| name == file))
            .ok_or_else(|| format!("built-in {id} missing {file}"))?;
        let contents = entry
            .as_file()
            .ok_or_else(|| format!("built-in {id}/{file} is not a file"))?
            .contents();
        std::fs::write(dest.join(file), contents).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundled_builtins_are_detected() {
        // These ship in the repo's tools/ dir.
        assert!(is_bundled("current-time"));
        assert!(is_bundled("calculate"));
        assert!(is_bundled("read-file"));
    }

    #[test]
    fn non_bundled_ids_rejected() {
        assert!(!is_bundled("definitely-not-a-tool"));
        assert!(!is_bundled("my-custom-tool"));
    }

    #[test]
    fn restore_overwrites_bundled_files() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        // Pre-create a modified manifest to confirm restore overwrites it.
        std::fs::create_dir_all(root.join("tools").join("calculate")).unwrap();
        std::fs::write(
            root.join("tools").join("calculate").join("manifest.json"),
            "MODIFIED",
        )
        .unwrap();
        restore(root, "calculate").unwrap();
        let restored = std::fs::read_to_string(
            root.join("tools").join("calculate").join("manifest.json"),
        )
        .unwrap();
        assert_ne!(restored, "MODIFIED");
        assert!(restored.contains("calculate"));
    }

    #[test]
    fn restore_rejects_non_bundled() {
        let dir = tempfile::tempdir().unwrap();
        assert!(restore(dir.path(), "my-custom-tool").is_err());
    }
}
