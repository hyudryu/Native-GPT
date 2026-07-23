//! Dedicated browser profile directories and host-level profile locks
//! (spec §6). Never launches Chromium against the user's own Chrome profile;
//! one Chromium process may write to a profile at a time.

use std::path::{Path, PathBuf};

/// Lock file name inside the profile directory.
const LOCK_FILE: &str = ".nativegpt.lock";

#[derive(Debug, thiserror::Error)]
pub enum ProfileError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("profile is locked by another running process (pid {0})")]
    Locked(u32),
    #[error("lock file is corrupt: {0}")]
    CorruptLock(String),
}

/// Browser data root: `AGENTGPT_DATA_DIR` when set (mirroring
/// [`crate::db::default_path`]), otherwise the platform app-data directory
/// from spec §6.1, falling back to `<repo_root>/app-data` in dev checkouts
/// where the platform location is not resolvable.
pub fn browser_data_root(repo_root: &Path) -> PathBuf {
    if let Ok(dir) = std::env::var("AGENTGPT_DATA_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir).join("browser");
        }
    }
    platform_app_data_dir().unwrap_or_else(|| repo_root.join("app-data").join("browser"))
}

/// Platform app-data directory per spec §6.1 (with the `browser` suffix).
fn platform_app_data_dir() -> Option<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .map(|p| p.join("Native GPT").join("browser"))
    }
    #[cfg(target_os = "macos")]
    {
        std::env::var_os("HOME").map(PathBuf::from).map(|p| {
            p.join("Library")
                .join("Application Support")
                .join("Native GPT")
                .join("browser")
        })
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        std::env::var_os("HOME").map(PathBuf::from).map(|p| {
            p.join(".local")
                .join("share")
                .join("native-gpt")
                .join("browser")
        })
    }
}

/// `<data-root>/profiles/<profile-id>` (spec §6.1).
///
/// `profile_id` is joined into the path unchecked; callers must only pass
/// ids that satisfy [`is_valid_profile_id`] (HTTP entry points validate).
pub fn profile_dir(data_root: &Path, profile_id: &str) -> PathBuf {
    data_root.join("profiles").join(profile_id)
}

/// Profile ids are path segments: lowercase UUIDs and names like `default`.
/// Anything containing separators, `.`/`..`, or non `[A-Za-z0-9_-]` bytes is
/// rejected so an id can never escape the `profiles/` root (path traversal).
pub fn is_valid_profile_id(profile_id: &str) -> bool {
    !profile_id.is_empty()
        && profile_id.len() <= 128
        && profile_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// `<profile-dir>/Downloads/` (spec §6.5).
pub fn downloads_dir(profile_dir: &Path) -> PathBuf {
    profile_dir.join("Downloads")
}

/// `<profile-dir>/Uploads/` (spec §6.5).
pub fn uploads_dir(profile_dir: &Path) -> PathBuf {
    profile_dir.join("Uploads")
}

#[derive(Debug, serde::Serialize, serde::Deserialize)]
struct LockContents {
    pid: u32,
    created_at: String,
}

/// Host-level profile lock. Held for as long as this process owns the
/// profile's Chromium process; released (file removed) on drop.
#[derive(Debug)]
pub struct ProfileLock {
    path: PathBuf,
}

impl ProfileLock {
    /// Acquire the lock for `profile_dir`, creating the directory first.
    /// A stale lock whose owning process no longer exists is taken over
    /// (spec §6.3).
    pub fn acquire(
        profile_dir: &Path,
        process_is_alive: impl Fn(u32) -> bool,
    ) -> Result<Self, ProfileError> {
        std::fs::create_dir_all(profile_dir)?;
        let path = profile_dir.join(LOCK_FILE);
        match write_new_lock(&path) {
            Ok(()) => Ok(Self { path }),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let pid = read_lock_pid(&path)?;
                if process_is_alive(pid) {
                    return Err(ProfileError::Locked(pid));
                }
                // Stale lock: previous holder died without cleanup.
                //
                // Known limitation (TOCTOU): if two processes detect the same
                // stale lock concurrently, both remove it and race on
                // `write_new_lock`. `create_new(true)` is atomic, so only one
                // wins, but there is a brief window where no process holds
                // the lock and a Chromium instance could start against the
                // profile unprotected. Acceptable for a single-host lock;
                // cross-host/profile NFS locking is out of scope.
                std::fs::remove_file(&path)?;
                write_new_lock(&path)?;
                Ok(Self { path })
            }
            Err(e) => Err(e.into()),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for ProfileLock {
    fn drop(&mut self) {
        // Best-effort: another process may already have taken over after a
        // crash; removing a file we no longer own is still safe because the
        // new holder re-creates it on acquire.
        let _ = std::fs::remove_file(&self.path);
    }
}

fn write_new_lock(path: &Path) -> std::io::Result<()> {
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    let contents = LockContents {
        pid: std::process::id(),
        created_at: chrono::Utc::now().to_rfc3339(),
    };
    let json = serde_json::to_string(&contents)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    file.write_all(json.as_bytes())
}

fn read_lock_pid(path: &Path) -> Result<u32, ProfileError> {
    let raw = std::fs::read_to_string(path)?;
    // Tolerate both the JSON form and a bare pid (older/manual locks).
    if let Ok(parsed) = serde_json::from_str::<LockContents>(&raw) {
        return Ok(parsed.pid);
    }
    raw.trim().parse::<u32>().map_err(|_| {
        // Keep the snippet debuggable: stop at the first line boundary, cap
        // at 64 chars (char-safe — byte slicing could split a multibyte char).
        let snippet: String = raw.lines().next().unwrap_or("").chars().take(64).collect();
        ProfileError::CorruptLock(snippet)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agentgpt-profile-test-{tag}-{}",
            uuid::Uuid::now_v7()
        ))
    }

    #[test]
    fn profile_id_validation_rejects_traversal() {
        assert!(is_valid_profile_id("default"));
        assert!(is_valid_profile_id(&uuid::Uuid::now_v7().to_string()));
        assert!(is_valid_profile_id("work_profile-2"));
        for bad in [
            "", "..", "../..", "a/b", "a\\b", "/etc", ".hidden", "a b", "a%2Fb",
        ] {
            assert!(!is_valid_profile_id(bad), "accepted {bad:?}");
        }
    }

    #[test]
    fn profile_dirs_resolve_under_data_root() {
        let root = Path::new("/data/browser");
        assert_eq!(
            profile_dir(root, "default"),
            PathBuf::from("/data/browser/profiles/default")
        );
        assert_eq!(
            downloads_dir(&profile_dir(root, "default")),
            PathBuf::from("/data/browser/profiles/default/Downloads")
        );
        assert_eq!(
            uploads_dir(&profile_dir(root, "default")),
            PathBuf::from("/data/browser/profiles/default/Uploads")
        );
    }

    #[test]
    fn lock_blocks_second_holder_while_process_alive() {
        let dir = temp_dir("alive");
        let alive = |_pid: u32| true;
        let lock = ProfileLock::acquire(&dir, alive).expect("first acquire");
        match ProfileLock::acquire(&dir, alive) {
            Err(ProfileError::Locked(pid)) => assert_eq!(pid, std::process::id()),
            other => panic!("expected Locked, got {other:?}"),
        }
        drop(lock);
        // Once released, a new holder can take it.
        let _lock2 = ProfileLock::acquire(&dir, alive).expect("re-acquire after drop");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn stale_lock_is_taken_over() {
        let dir = temp_dir("stale");
        let dead = |_pid: u32| false;
        let lock = ProfileLock::acquire(&dir, dead).expect("first acquire");
        // Simulate an unclean exit: leak the lock file (forget, not drop).
        let lock_path = lock.path().to_path_buf();
        std::mem::forget(lock);
        assert!(lock_path.is_file());
        // The "dead process" predicate lets the second holder take over.
        let lock2 = ProfileLock::acquire(&dir, dead).expect("stale takeover");
        assert!(lock_path.is_file());
        drop(lock2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn corrupt_lock_reports_error() {
        let dir = temp_dir("corrupt");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(LOCK_FILE), "not-a-pid-not-json").unwrap();
        match ProfileLock::acquire(&dir, |_| false) {
            Err(ProfileError::CorruptLock(_)) => {}
            other => panic!("expected CorruptLock, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bare_pid_lock_files_are_understood() {
        let dir = temp_dir("barepid");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(LOCK_FILE), "4242").unwrap();
        match ProfileLock::acquire(&dir, |pid| pid == 4242) {
            Err(ProfileError::Locked(4242)) => {}
            other => panic!("expected Locked(4242), got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }
}
