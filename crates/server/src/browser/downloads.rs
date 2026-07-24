//! Download tracking (spec §6.5): CDP download events are recorded in
//! `browser_downloads`, files land in the profile's `Downloads/` directory,
//! filenames are sanitized, and `browser.download` events reach viewers.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use tokio::sync::broadcast;
use tracing::warn;

use crate::db::{BrowserDownloadRow, Db};

use super::protocol::StreamEvent;

/// Windows-reserved device names that must not be used as filenames.
const RESERVED_NAMES: &[&str] = &[
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8",
    "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

/// Sanitize a suggested download filename: strip path components, control
/// characters and characters illegal on Windows, dodge reserved device
/// names, and cap length. Returns "download" when nothing usable remains.
pub fn sanitize_filename(suggested: &str) -> String {
    // Keep only the final path component (both separators, either platform).
    let name = suggested
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(suggested)
        .trim();
    let mut cleaned: String = name
        .chars()
        .map(|c| match c {
            '<' | '>' | ':' | '"' | '|' | '?' | '*' => '_',
            c if c.is_control() => '_',
            c => c,
        })
        .collect();
    // No trailing dots or spaces (Windows strips them, causing collisions).
    while cleaned.ends_with(['.', ' ']) {
        cleaned.pop();
    }
    if cleaned.starts_with('.') {
        cleaned = format!("_{}", cleaned.trim_start_matches('.'));
    }
    const MAX_LEN: usize = 120;
    if cleaned.chars().count() > MAX_LEN {
        // Preserve the extension when truncating.
        let ext = Path::new(&cleaned)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_string());
        let mut truncated: String = cleaned.chars().take(MAX_LEN).collect();
        if let Some(ext) = ext {
            let suffix = format!(".{ext}");
            if !truncated.ends_with(&suffix) {
                let keep = MAX_LEN.saturating_sub(suffix.chars().count());
                truncated = cleaned.chars().take(keep).collect();
                truncated.push_str(&suffix);
            }
        }
        cleaned = truncated;
    }
    if cleaned.is_empty() {
        return "download".to_string();
    }
    let stem = cleaned.split('.').next().unwrap_or("").to_ascii_lowercase();
    if RESERVED_NAMES.contains(&stem.as_str()) {
        cleaned = format!("_{cleaned}");
    }
    cleaned
}

/// Pick a non-colliding path in `dir` for `filename` ("name (1).ext", …).
pub fn unique_path(dir: &Path, filename: &str) -> PathBuf {
    let candidate = dir.join(filename);
    if !candidate.exists() {
        return candidate;
    }
    let path = Path::new(filename);
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("download");
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{e}"))
        .unwrap_or_default();
    for n in 1..1000u32 {
        let candidate = dir.join(format!("{stem} ({n}){ext}"));
        if !candidate.exists() {
            return candidate;
        }
    }
    candidate
}

/// Tracks in-flight CDP downloads for one profile.
pub struct DownloadTracker {
    db: Db,
    profile_id: String,
    events: broadcast::Sender<StreamEvent>,
}

impl DownloadTracker {
    pub fn new(db: Db, profile_id: String, events: broadcast::Sender<StreamEvent>) -> Self {
        Self {
            db,
            profile_id,
            events,
        }
    }

    /// `Browser.downloadWillBegin` → insert an `in_progress` row.
    pub async fn will_begin(
        &self,
        guid: &str,
        suggested_filename: &str,
        source_url: Option<&str>,
        download_dir: &Path,
        task_id: Option<&str>,
    ) -> Option<BrowserDownloadRow> {
        let filename = sanitize_filename(suggested_filename);
        let local_path = unique_path(download_dir, &filename);
        let row = BrowserDownloadRow {
            id: guid.to_string(),
            profile_id: self.profile_id.clone(),
            task_id: task_id.map(str::to_string),
            source_url: source_url.map(str::to_string),
            filename,
            local_path: local_path.to_string_lossy().into_owned(),
            mime_type: None,
            size_bytes: None,
            status: "in_progress".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        if let Err(e) = self.db.insert_browser_download(&row).await {
            warn!(error = %e, "failed to record browser download");
            return None;
        }
        self.emit(&row);
        Some(row)
    }

    /// `Browser.downloadProgress` → update status (completed/canceled) and size.
    pub async fn progress(&self, guid: &str, state: &str, received_bytes: Option<i64>) {
        let status = match state {
            "completed" => "completed",
            "canceled" => "cancelled",
            _ => "in_progress",
        };
        if let Err(e) = self
            .db
            .update_browser_download(guid, status, received_bytes)
            .await
        {
            warn!(error = %e, "failed to update browser download");
        }
        if status != "in_progress" {
            self.emit_raw(json!({
                "id": guid,
                "status": status,
                "sizeBytes": received_bytes,
            }));
        }
    }

    fn emit(&self, row: &BrowserDownloadRow) {
        self.emit_raw(json!({
            "id": row.id,
            "profileId": row.profile_id,
            "filename": row.filename,
            "status": row.status,
            "sourceUrl": row.source_url,
        }));
    }

    fn emit_raw(&self, payload: Value) {
        let _ = self.events.send(StreamEvent::Download(payload));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizes_filenames() {
        assert_eq!(sanitize_filename("report.pdf"), "report.pdf");
        assert_eq!(
            sanitize_filename("C:\\Users\\evil\\..\\secret.txt"),
            "secret.txt"
        );
        assert_eq!(sanitize_filename("/etc/passwd"), "passwd");
        assert_eq!(
            sanitize_filename("a<b>c:d\"e|f?g*h.txt"),
            "a_b_c_d_e_f_g_h.txt"
        );
        assert_eq!(sanitize_filename("..."), "download");
        assert_eq!(sanitize_filename(""), "download");
        assert_eq!(sanitize_filename("CON"), "_CON");
        assert_eq!(sanitize_filename("nul.txt"), "_nul.txt");
        assert_eq!(sanitize_filename("name."), "name");
        assert_eq!(sanitize_filename("  spaced  "), "spaced");
        assert_eq!(sanitize_filename(".hidden"), "_hidden");
        let long = format!("{}.txt", "x".repeat(300));
        let sanitized = sanitize_filename(&long);
        assert!(sanitized.chars().count() <= 120);
        assert!(sanitized.ends_with(".txt"));
    }

    #[test]
    fn unique_path_avoids_collisions() {
        let dir = std::env::temp_dir().join(format!("agentgpt-dl-test-{}", uuid::Uuid::now_v7()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("file.txt"), b"a").unwrap();
        std::fs::write(dir.join("file (1).txt"), b"b").unwrap();
        let path = unique_path(&dir, "file.txt");
        assert!(path.ends_with("file (2).txt"));
        let fresh = unique_path(&dir, "fresh.txt");
        assert!(fresh.ends_with("fresh.txt"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
