//! SQLite persistence for endpoints and models (ADR-0006).
//!
//! `rusqlite` with bundled SQLite, WAL mode, foreign keys on. Migrations are
//! numbered SQL files embedded at compile time from `crates/server/migrations/`
//! and applied idempotently via a `schema_migrations` table. All calls run on
//! `tokio::task::spawn_blocking` (rusqlite is sync).

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;

/// Embedded migrations, applied in order. Names are recorded in
/// `schema_migrations` so re-runs are no-ops.
const MIGRATIONS: &[(&str, &str)] = &[
    (
        "0001_endpoints",
        include_str!("../migrations/0001_endpoints.sql"),
    ),
    ("0002_phase3", include_str!("../migrations/0002_phase3.sql")),
    (
        "0003_app_hub",
        include_str!("../migrations/0003_app_hub.sql"),
    ),
    (
        "0004_tool_events",
        include_str!("../migrations/0004_tool_events.sql"),
    ),
    (
        "0005_project_knowledge",
        include_str!("../migrations/0005_project_knowledge.sql"),
    ),
    (
        "0006_remote_hosts",
        include_str!("../migrations/0006_remote_hosts.sql"),
    ),
    (
        "0007_generated_assets",
        include_str!("../migrations/0007_generated_assets.sql"),
    ),
    ("0008_voices", include_str!("../migrations/0008_voices.sql")),
    (
        "0009_browser",
        include_str!("../migrations/0009_browser.sql"),
    ),
];

#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("database error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("database task failed: {0}")]
    Task(String),
}

/// Database file location: `AGENTGPT_DATA_DIR` if set, else
/// `<repo_root>/app-data/database/agentgpt.sqlite3`.
pub fn default_path(repo_root: &Path) -> PathBuf {
    if let Ok(dir) = std::env::var("AGENTGPT_DATA_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir).join("agentgpt.sqlite3");
        }
    }
    repo_root
        .join("app-data")
        .join("database")
        .join("agentgpt.sqlite3")
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

/// Apply any pending migrations. Idempotent.
fn migrate(conn: &Connection) -> Result<(), DbError> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );",
    )?;
    let applied: HashSet<String> = {
        let mut stmt = conn.prepare("SELECT name FROM schema_migrations")?;
        let rows = stmt.query_map([], |row| row.get(0))?;
        rows.collect::<Result<_, _>>()?
    };
    for (name, sql) in MIGRATIONS {
        if applied.contains(*name) {
            continue;
        }
        conn.execute_batch("BEGIN")?;
        let result = conn
            .execute_batch(sql)
            .and_then(|()| {
                conn.execute(
                    "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                    params![name, chrono::Utc::now().to_rfc3339()],
                )
                .map(|_| ())
            })
            .and_then(|()| conn.execute_batch("COMMIT"));
        if let Err(e) = result {
            let _ = conn.execute_batch("ROLLBACK");
            return Err(e.into());
        }
    }
    Ok(())
}

/// Shared handle to the SQLite database. Cheap to clone.
#[derive(Clone)]
pub struct Db {
    conn: Arc<Mutex<Connection>>,
}

/// Row of the `endpoints` table; serialized directly as the REST shape.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct EndpointRow {
    pub id: String,
    pub name: String,
    pub base_url: String,
    pub timeout_seconds: i64,
    pub tls_verify: bool,
    pub has_api_key: bool,
    pub default_model_id: Option<String>,
    pub last_test_status: Option<String>,
    pub last_tested_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

/// Row of the `models` table.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelRow {
    pub id: String,
    pub endpoint_id: String,
    pub remote_model_id: String,
    pub source: String,
    pub hidden: bool,
    pub capabilities_json: Option<String>,
    pub raw_json: Option<String>,
    pub last_seen_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProjectRow {
    pub id: String,
    pub name: String,
    pub instructions: String,
    pub endpoint_id: Option<String>,
    pub model_id: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ConversationRow {
    pub id: String,
    pub project_id: Option<String>,
    pub title: String,
    pub endpoint_id: Option<String>,
    pub model_id: Option<String>,
    pub archived_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    /// Number of messages in the conversation. Only populated by
    /// `list_conversations` (via a correlated COUNT subquery); left `None`
    /// elsewhere so inserts/updates don't need to compute it. Serialized
    /// only when present so other responses are unchanged.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message_count: Option<i64>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct MessageRow {
    pub id: String,
    pub conversation_id: String,
    pub role: String,
    pub content: String,
    pub status: String,
    pub created_at: String,
    /// JSON-serialized tool-call trace for runs that emitted any. NULL for
    /// user/system messages and assistant messages from runs without tools.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_events_json: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RunRow {
    pub id: String,
    pub conversation_id: String,
    pub user_message_id: Option<String>,
    pub assistant_message_id: Option<String>,
    pub status: String,
    pub endpoint_id: Option<String>,
    pub model_id: Option<String>,
    pub started_at: String,
    pub completed_at: Option<String>,
    pub usage_json: Option<String>,
    pub error_json: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct KnowledgeSourceRow {
    pub id: String,
    pub title: String,
    pub source_type: String,
    pub source_uri: Option<String>,
    #[serde(skip_serializing)]
    pub content: String,
    pub chunk_count: i64,
    pub created_at: String,
    pub updated_at: String,
    /// NULL = global source (available to all chats); non-null = scoped to
    /// that project only. See migration 0005.
    pub project_id: Option<String>,
}

/// Row of the `remote_hosts` table; serialized directly as the REST shape.
/// The raw bearer token lives only in the keychain under key `host:<id>`.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RemoteHostRow {
    pub id: String,
    pub name: String,
    pub base_url: String,
    pub tls_verify: bool,
    pub has_token: bool,
    pub status: Option<String>,
    pub last_checked_at: Option<String>,
    pub workloads_json: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

/// Row of the `generated_assets` table; serialized directly as the REST shape.
/// Bytes live on disk under `app-data/assets/`; this table holds metadata.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct GeneratedAssetRow {
    pub id: String,
    pub host_id: String,
    pub workload: String,
    pub kind: String,
    pub message_id: Option<String>,
    pub prompt_text: Option<String>,
    pub source_ref: Option<String>,
    pub storage_path: String,
    pub bytes: Option<i64>,
    pub mime_type: Option<String>,
    pub created_at: String,
}

/// Row of the `voices` table; serialized directly as the REST shape.
/// The raw clip and extracted embedding live on the bridge host.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct VoiceRow {
    pub id: String,
    pub name: String,
    pub host_id: String,
    pub source_kind: String,
    pub source_ref: Option<String>,
    pub duration_ms: Option<i64>,
    pub created_at: String,
    pub last_used_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct KnowledgeChunkRow {
    pub id: String,
    pub source_id: String,
    pub source_title: String,
    pub position: i64,
    pub content: String,
    pub embedding_json: String,
    pub created_at: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AnalyticsRunRow {
    pub provider_name: String,
    pub model_id: String,
    pub status: String,
    pub started_at: String,
    pub completed_at: Option<String>,
    pub usage_json: Option<String>,
}

/// Row of the `browser_profiles` table (spec §13). `profile_path` may be
/// empty for the seeded default profile; it is resolved lazily at runtime.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BrowserProfileRow {
    pub id: String,
    pub name: String,
    pub engine: String,
    pub executable_path: Option<String>,
    pub profile_path: String,
    pub created_at: String,
    pub updated_at: String,
    pub last_used_at: Option<String>,
}

/// Row of the `browser_preferences` table; serialized directly as the REST
/// shape (camelCase applied by the browser handlers, not here).
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BrowserPreferencesRow {
    pub profile_id: String,
    pub panel_mode: String,
    pub panel_width: i64,
    pub previous_panel_width: Option<i64>,
    pub auto_open_on_tool_call: bool,
    pub keep_running_when_hidden: bool,
    pub remote_streaming_enabled: bool,
    pub model_mode: String,
    pub model_endpoint_id: Option<String>,
    pub model_id: Option<String>,
}

/// Row of the `browser_tasks` audit table.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BrowserTaskRow {
    pub id: String,
    pub profile_id: String,
    pub conversation_id: Option<String>,
    pub run_id: Option<String>,
    pub tool_call_id: Option<String>,
    pub task_text: String,
    pub initial_url: Option<String>,
    pub final_url: Option<String>,
    pub status: String,
    pub result_text: Option<String>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub started_at: String,
    pub finished_at: Option<String>,
}

/// Row of the `browser_permissions` table.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BrowserPermissionRow {
    pub id: String,
    pub profile_id: String,
    pub origin: Option<String>,
    pub capability: String,
    pub scope: String,
    pub conversation_id: Option<String>,
    pub expires_at: Option<String>,
    pub created_at: String,
}

/// Row of the `browser_downloads` table.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct BrowserDownloadRow {
    pub id: String,
    pub profile_id: String,
    pub task_id: Option<String>,
    pub source_url: Option<String>,
    pub filename: String,
    pub local_path: String,
    pub mime_type: Option<String>,
    pub size_bytes: Option<i64>,
    pub status: String,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProviderModelRow {
    pub provider_id: String,
    pub provider_name: String,
    pub provider_url: String,
    pub model_id: String,
    pub enabled: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ResolvedModel {
    pub provider_id: String,
    pub provider_name: String,
    pub provider_url: String,
    pub model_id: String,
    /// The provider's TLS verification setting, forwarded to the sidecar so
    /// chat runs honor it just like endpoint tests and model discovery do.
    pub tls_verify: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum ModelResolutionError {
    #[error(transparent)]
    Database(#[from] DbError),
    #[error("conversation {0} not found")]
    ConversationNotFound(String),
    #[error("no provider/model is configured for conversation {0}")]
    NotConfigured(String),
    #[error("provider {0} not found")]
    ProviderNotFound(String),
    #[error("provider {0} has no default model")]
    ProviderDefaultMissing(String),
    #[error("model {model_id} was not found for provider {provider_id}")]
    ModelNotFound {
        provider_id: String,
        model_id: String,
    },
    #[error("model {model_id} is disabled for provider {provider_id}")]
    ModelDisabled {
        provider_id: String,
        model_id: String,
    },
}

fn endpoint_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<EndpointRow> {
    Ok(EndpointRow {
        id: row.get("id")?,
        name: row.get("name")?,
        base_url: row.get("base_url")?,
        timeout_seconds: row.get("timeout_seconds")?,
        tls_verify: row.get("tls_verify")?,
        has_api_key: row.get("has_api_key")?,
        default_model_id: row.get("default_model_id")?,
        last_test_status: row.get("last_test_status")?,
        last_tested_at: row.get("last_tested_at")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
    })
}

fn model_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ModelRow> {
    Ok(ModelRow {
        id: row.get("id")?,
        endpoint_id: row.get("endpoint_id")?,
        remote_model_id: row.get("remote_model_id")?,
        source: row.get("source")?,
        hidden: row.get("hidden")?,
        capabilities_json: row.get("capabilities_json")?,
        raw_json: row.get("raw_json")?,
        last_seen_at: row.get("last_seen_at")?,
    })
}

fn project_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ProjectRow> {
    Ok(ProjectRow {
        id: row.get("id")?,
        name: row.get("name")?,
        instructions: row.get("instructions")?,
        endpoint_id: row.get("endpoint_id")?,
        model_id: row.get("model_id")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
    })
}

fn conversation_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<ConversationRow> {
    Ok(ConversationRow {
        id: row.get("id")?,
        project_id: row.get("project_id")?,
        title: row.get("title")?,
        endpoint_id: row.get("endpoint_id")?,
        model_id: row.get("model_id")?,
        archived_at: row.get("archived_at")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        // Only present when the query includes it (list_conversations adds a
        // `message_count` column); fall back to None otherwise so the shared
        // mapper works for get/insert/update paths too.
        message_count: row.get("message_count").unwrap_or(None),
    })
}

fn message_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<MessageRow> {
    Ok(MessageRow {
        id: row.get("id")?,
        conversation_id: row.get("conversation_id")?,
        role: row.get("role")?,
        content: row.get("content")?,
        status: row.get("status")?,
        created_at: row.get("created_at")?,
        tool_events_json: row.get("tool_events_json")?,
    })
}

fn run_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<RunRow> {
    Ok(RunRow {
        id: row.get("id")?,
        conversation_id: row.get("conversation_id")?,
        user_message_id: row.get("user_message_id")?,
        assistant_message_id: row.get("assistant_message_id")?,
        status: row.get("status")?,
        endpoint_id: row.get("endpoint_id")?,
        model_id: row.get("model_id")?,
        started_at: row.get("started_at")?,
        completed_at: row.get("completed_at")?,
        usage_json: row.get("usage_json")?,
        error_json: row.get("error_json")?,
    })
}

fn knowledge_source_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<KnowledgeSourceRow> {
    Ok(KnowledgeSourceRow {
        id: row.get("id")?,
        title: row.get("title")?,
        source_type: row.get("source_type")?,
        source_uri: row.get("source_uri")?,
        content: row.get("content")?,
        chunk_count: row.get("chunk_count")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        project_id: row.get("project_id")?,
    })
}

fn knowledge_chunk_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<KnowledgeChunkRow> {
    Ok(KnowledgeChunkRow {
        id: row.get(0)?,
        source_id: row.get(1)?,
        source_title: row.get(2)?,
        position: row.get(3)?,
        content: row.get(4)?,
        embedding_json: row.get(5)?,
        created_at: row.get(6)?,
    })
}

const ENDPOINT_COLUMNS: &str = "id, name, base_url, timeout_seconds, tls_verify, has_api_key, \
     default_model_id, last_test_status, last_tested_at, created_at, updated_at";

const MODEL_COLUMNS: &str =
    "id, endpoint_id, remote_model_id, source, hidden, capabilities_json, raw_json, last_seen_at";

const PROJECT_COLUMNS: &str =
    "id, name, instructions, endpoint_id, model_id, created_at, updated_at";
const CONVERSATION_COLUMNS: &str =
    "id, project_id, title, endpoint_id, model_id, archived_at, created_at, updated_at";
const MESSAGE_COLUMNS: &str =
    "id, conversation_id, role, content, status, created_at, tool_events_json";
const RUN_COLUMNS: &str = "id, conversation_id, user_message_id, assistant_message_id, status, \
    endpoint_id, model_id, started_at, completed_at, usage_json, error_json";
const KNOWLEDGE_SOURCE_COLUMNS: &str =
    "id, title, source_type, source_uri, content, chunk_count, created_at, updated_at, project_id";

const REMOTE_HOST_COLUMNS: &str =
    "id, name, base_url, tls_verify, has_token, status, last_checked_at, workloads_json, \
     created_at, updated_at";

const GENERATED_ASSET_COLUMNS: &str =
    "id, host_id, workload, kind, message_id, prompt_text, source_ref, storage_path, bytes, \
     mime_type, created_at";

const VOICE_COLUMNS: &str =
    "id, name, host_id, source_kind, source_ref, duration_ms, created_at, last_used_at";

const BROWSER_PROFILE_COLUMNS: &str =
    "id, name, engine, executable_path, profile_path, created_at, updated_at, last_used_at";
const BROWSER_PREFERENCES_COLUMNS: &str =
    "profile_id, panel_mode, panel_width, previous_panel_width, auto_open_on_tool_call, \
     keep_running_when_hidden, remote_streaming_enabled, model_mode, model_endpoint_id, model_id";
const BROWSER_TASK_COLUMNS: &str =
    "id, profile_id, conversation_id, run_id, tool_call_id, task_text, initial_url, final_url, \
     status, result_text, error_code, error_message, started_at, finished_at";
const BROWSER_PERMISSION_COLUMNS: &str =
    "id, profile_id, origin, capability, scope, conversation_id, expires_at, created_at";
const BROWSER_DOWNLOAD_COLUMNS: &str =
    "id, profile_id, task_id, source_url, filename, local_path, mime_type, size_bytes, status, \
     created_at";

fn browser_profile_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<BrowserProfileRow> {
    Ok(BrowserProfileRow {
        id: row.get("id")?,
        name: row.get("name")?,
        engine: row.get("engine")?,
        executable_path: row.get("executable_path")?,
        profile_path: row.get("profile_path")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        last_used_at: row.get("last_used_at")?,
    })
}

fn browser_preferences_from_row(
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<BrowserPreferencesRow> {
    Ok(BrowserPreferencesRow {
        profile_id: row.get("profile_id")?,
        panel_mode: row.get("panel_mode")?,
        panel_width: row.get("panel_width")?,
        previous_panel_width: row.get("previous_panel_width")?,
        auto_open_on_tool_call: row.get("auto_open_on_tool_call")?,
        keep_running_when_hidden: row.get("keep_running_when_hidden")?,
        remote_streaming_enabled: row.get("remote_streaming_enabled")?,
        model_mode: row.get("model_mode")?,
        model_endpoint_id: row.get("model_endpoint_id")?,
        model_id: row.get("model_id")?,
    })
}

fn browser_task_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<BrowserTaskRow> {
    Ok(BrowserTaskRow {
        id: row.get("id")?,
        profile_id: row.get("profile_id")?,
        conversation_id: row.get("conversation_id")?,
        run_id: row.get("run_id")?,
        tool_call_id: row.get("tool_call_id")?,
        task_text: row.get("task_text")?,
        initial_url: row.get("initial_url")?,
        final_url: row.get("final_url")?,
        status: row.get("status")?,
        result_text: row.get("result_text")?,
        error_code: row.get("error_code")?,
        error_message: row.get("error_message")?,
        started_at: row.get("started_at")?,
        finished_at: row.get("finished_at")?,
    })
}

fn browser_permission_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<BrowserPermissionRow> {
    Ok(BrowserPermissionRow {
        id: row.get("id")?,
        profile_id: row.get("profile_id")?,
        origin: row.get("origin")?,
        capability: row.get("capability")?,
        scope: row.get("scope")?,
        conversation_id: row.get("conversation_id")?,
        expires_at: row.get("expires_at")?,
        created_at: row.get("created_at")?,
    })
}

fn browser_download_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<BrowserDownloadRow> {
    Ok(BrowserDownloadRow {
        id: row.get("id")?,
        profile_id: row.get("profile_id")?,
        task_id: row.get("task_id")?,
        source_url: row.get("source_url")?,
        filename: row.get("filename")?,
        local_path: row.get("local_path")?,
        mime_type: row.get("mime_type")?,
        size_bytes: row.get("size_bytes")?,
        status: row.get("status")?,
        created_at: row.get("created_at")?,
    })
}

fn remote_host_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<RemoteHostRow> {
    Ok(RemoteHostRow {
        id: row.get("id")?,
        name: row.get("name")?,
        base_url: row.get("base_url")?,
        tls_verify: row.get("tls_verify")?,
        has_token: row.get("has_token")?,
        status: row.get("status")?,
        last_checked_at: row.get("last_checked_at")?,
        workloads_json: row.get("workloads_json")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
    })
}

fn generated_asset_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<GeneratedAssetRow> {
    Ok(GeneratedAssetRow {
        id: row.get("id")?,
        host_id: row.get("host_id")?,
        workload: row.get("workload")?,
        kind: row.get("kind")?,
        message_id: row.get("message_id")?,
        prompt_text: row.get("prompt_text")?,
        source_ref: row.get("source_ref")?,
        storage_path: row.get("storage_path")?,
        bytes: row.get("bytes")?,
        mime_type: row.get("mime_type")?,
        created_at: row.get("created_at")?,
    })
}

fn voice_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<VoiceRow> {
    Ok(VoiceRow {
        id: row.get("id")?,
        name: row.get("name")?,
        host_id: row.get("host_id")?,
        source_kind: row.get("source_kind")?,
        source_ref: row.get("source_ref")?,
        duration_ms: row.get("duration_ms")?,
        created_at: row.get("created_at")?,
        last_used_at: row.get("last_used_at")?,
    })
}

impl Db {
    /// Open (creating parent dirs), set pragmas, run pending migrations.
    pub fn open(path: &Path) -> Result<Self, DbError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;
        migrate(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    /// Run `f` with the connection on a blocking thread.
    async fn call<F, T>(&self, f: F) -> Result<T, DbError>
    where
        F: FnOnce(&Connection) -> Result<T, DbError> + Send + 'static,
        T: Send + 'static,
    {
        let conn = self.conn.clone();
        tokio::task::spawn_blocking(move || {
            let guard = lock(&conn);
            f(&guard)
        })
        .await
        .map_err(|e| DbError::Task(e.to_string()))?
    }

    pub async fn insert_endpoint(&self, endpoint: &EndpointRow) -> Result<(), DbError> {
        let e = endpoint.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO endpoints ({ENDPOINT_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    e.id,
                    e.name,
                    e.base_url,
                    e.timeout_seconds,
                    e.tls_verify,
                    e.has_api_key,
                    e.default_model_id,
                    e.last_test_status,
                    e.last_tested_at,
                    e.created_at,
                    e.updated_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_endpoints(&self) -> Result<Vec<EndpointRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {ENDPOINT_COLUMNS} FROM endpoints ORDER BY created_at"
            ))?;
            let rows = stmt
                .query_map([], endpoint_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn get_endpoint(&self, id: &str) -> Result<Option<EndpointRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {ENDPOINT_COLUMNS} FROM endpoints WHERE id = ?"
            ))?;
            let mut rows = stmt.query_map(params![id], endpoint_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    /// Overwrite an endpoint row (read-modify-write happens in the handler).
    pub async fn update_endpoint(&self, endpoint: &EndpointRow) -> Result<(), DbError> {
        let e = endpoint.clone();
        self.call(move |conn| {
            conn.execute(
                "UPDATE endpoints SET name = ?, base_url = ?, timeout_seconds = ?, \
                 tls_verify = ?, has_api_key = ?, default_model_id = ?, \
                 last_test_status = ?, last_tested_at = ?, updated_at = ? \
                 WHERE id = ?",
                params![
                    e.name,
                    e.base_url,
                    e.timeout_seconds,
                    e.tls_verify,
                    e.has_api_key,
                    e.default_model_id,
                    e.last_test_status,
                    e.last_tested_at,
                    e.updated_at,
                    e.id,
                ],
            )?;
            Ok(())
        })
        .await
    }

    /// Delete an endpoint; models cascade (foreign keys on).
    pub async fn delete_endpoint(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let n = conn.execute("DELETE FROM endpoints WHERE id = ?", params![id])?;
            Ok(n > 0)
        })
        .await
    }

    pub async fn update_test_status(
        &self,
        id: &str,
        status: &str,
        tested_at: &str,
    ) -> Result<(), DbError> {
        let (id, status, tested_at) = (id.to_string(), status.to_string(), tested_at.to_string());
        self.call(move |conn| {
            conn.execute(
                "UPDATE endpoints SET last_test_status = ?, last_tested_at = ? WHERE id = ?",
                params![status, tested_at, id],
            )?;
            Ok(())
        })
        .await
    }

    // ---- remote_hosts ----

    pub async fn insert_remote_host(&self, host: &RemoteHostRow) -> Result<(), DbError> {
        let h = host.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO remote_hosts ({REMOTE_HOST_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    h.id,
                    h.name,
                    h.base_url,
                    h.tls_verify,
                    h.has_token,
                    h.status,
                    h.last_checked_at,
                    h.workloads_json,
                    h.created_at,
                    h.updated_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_remote_hosts(&self) -> Result<Vec<RemoteHostRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {REMOTE_HOST_COLUMNS} FROM remote_hosts ORDER BY created_at"
            ))?;
            let rows = stmt
                .query_map([], remote_host_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn get_remote_host(&self, id: &str) -> Result<Option<RemoteHostRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {REMOTE_HOST_COLUMNS} FROM remote_hosts WHERE id = ?"
            ))?;
            let mut rows = stmt.query_map(params![id], remote_host_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    /// Overwrite a remote_hosts row (read-modify-write happens in the handler).
    pub async fn update_remote_host(&self, host: &RemoteHostRow) -> Result<(), DbError> {
        let h = host.clone();
        self.call(move |conn| {
            conn.execute(
                "UPDATE remote_hosts SET name = ?, base_url = ?, tls_verify = ?, has_token = ?, \
                 status = ?, last_checked_at = ?, workloads_json = ?, updated_at = ? WHERE id = ?",
                params![
                    h.name,
                    h.base_url,
                    h.tls_verify,
                    h.has_token,
                    h.status,
                    h.last_checked_at,
                    h.workloads_json,
                    h.updated_at,
                    h.id,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn delete_remote_host(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let n = conn.execute("DELETE FROM remote_hosts WHERE id = ?", params![id])?;
            Ok(n > 0)
        })
        .await
    }

    /// Update cached reachability + capability snapshot from a `/health` probe.
    pub async fn update_remote_host_status(
        &self,
        id: &str,
        status: &str,
        workloads_json: Option<&str>,
        checked_at: &str,
    ) -> Result<(), DbError> {
        let (id, status, workloads_json, checked_at) = (
            id.to_string(),
            status.to_string(),
            workloads_json.map(|s| s.to_string()),
            checked_at.to_string(),
        );
        self.call(move |conn| {
            conn.execute(
                "UPDATE remote_hosts SET status = ?, workloads_json = ?, last_checked_at = ?, \
                 updated_at = ? WHERE id = ?",
                params![status, workloads_json, checked_at, checked_at, id],
            )?;
            Ok(())
        })
        .await
    }

    // ---- generated_assets ----

    pub async fn insert_asset(&self, asset: &GeneratedAssetRow) -> Result<(), DbError> {
        let a = asset.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO generated_assets ({GENERATED_ASSET_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    a.id,
                    a.host_id,
                    a.workload,
                    a.kind,
                    a.message_id,
                    a.prompt_text,
                    a.source_ref,
                    a.storage_path,
                    a.bytes,
                    a.mime_type,
                    a.created_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn get_asset(&self, id: &str) -> Result<Option<GeneratedAssetRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {GENERATED_ASSET_COLUMNS} FROM generated_assets WHERE id = ?"
            ))?;
            let mut rows = stmt.query_map(params![id], generated_asset_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    /// List asset storage paths for a host (for file cleanup on host delete).
    pub async fn list_asset_paths_by_host(&self, host_id: &str) -> Result<Vec<String>, DbError> {
        let host_id = host_id.to_string();
        self.call(move |conn| {
            let mut stmt =
                conn.prepare("SELECT storage_path FROM generated_assets WHERE host_id = ?")?;
            let rows = stmt
                .query_map(params![host_id], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    // ---- voices ----

    pub async fn insert_voice(&self, voice: &VoiceRow) -> Result<(), DbError> {
        let v = voice.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO voices ({VOICE_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    v.id,
                    v.name,
                    v.host_id,
                    v.source_kind,
                    v.source_ref,
                    v.duration_ms,
                    v.created_at,
                    v.last_used_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_voices(&self, host_id: &str) -> Result<Vec<VoiceRow>, DbError> {
        let host_id = host_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {VOICE_COLUMNS} FROM voices WHERE host_id = ? ORDER BY created_at"
            ))?;
            let rows = stmt
                .query_map(params![host_id], voice_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn get_voice(&self, id: &str) -> Result<Option<VoiceRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let mut stmt =
                conn.prepare(&format!("SELECT {VOICE_COLUMNS} FROM voices WHERE id = ?"))?;
            let mut rows = stmt.query_map(params![id], voice_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    pub async fn delete_voice(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let n = conn.execute("DELETE FROM voices WHERE id = ?", params![id])?;
            Ok(n > 0)
        })
        .await
    }

    pub async fn touch_voice(&self, id: &str, used_at: &str) -> Result<(), DbError> {
        let (id, used_at) = (id.to_string(), used_at.to_string());
        self.call(move |conn| {
            conn.execute(
                "UPDATE voices SET last_used_at = ? WHERE id = ?",
                params![used_at, id],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_models(&self, endpoint_id: &str) -> Result<Vec<ModelRow>, DbError> {
        let endpoint_id = endpoint_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {MODEL_COLUMNS} FROM models WHERE endpoint_id = ? \
                 ORDER BY remote_model_id"
            ))?;
            let rows = stmt
                .query_map(params![endpoint_id], model_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn get_model(
        &self,
        endpoint_id: &str,
        remote_model_id: &str,
    ) -> Result<Option<ModelRow>, DbError> {
        let (endpoint_id, remote_model_id) = (endpoint_id.to_string(), remote_model_id.to_string());
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {MODEL_COLUMNS} FROM models \
                 WHERE endpoint_id = ? AND remote_model_id = ?"
            ))?;
            let mut rows = stmt.query_map(params![endpoint_id, remote_model_id], model_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    /// Insert or refresh a model row. `hidden` and `source` survive upserts.
    pub async fn upsert_model(
        &self,
        endpoint_id: &str,
        remote_model_id: &str,
        source: &str,
        raw_json: Option<&str>,
    ) -> Result<ModelRow, DbError> {
        let (endpoint_id, remote_model_id, source) = (
            endpoint_id.to_string(),
            remote_model_id.to_string(),
            source.to_string(),
        );
        let raw_json = raw_json.map(str::to_string);
        self.call(move |conn| {
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                &format!(
                    "INSERT INTO models ({MODEL_COLUMNS}) \
                     VALUES (?, ?, ?, ?, 0, NULL, ?, ?) \
                     ON CONFLICT(endpoint_id, remote_model_id) DO UPDATE SET \
                         raw_json = excluded.raw_json, \
                         last_seen_at = excluded.last_seen_at"
                ),
                params![
                    uuid::Uuid::now_v7().to_string(),
                    endpoint_id,
                    remote_model_id,
                    source,
                    raw_json,
                    now,
                ],
            )?;
            let mut stmt = conn.prepare(&format!(
                "SELECT {MODEL_COLUMNS} FROM models \
                 WHERE endpoint_id = ? AND remote_model_id = ?"
            ))?;
            let row = stmt.query_row(params![endpoint_id, remote_model_id], model_from_row)?;
            Ok(row)
        })
        .await
    }

    /// Replace the discovered model set: upsert `models` (preserving `hidden`
    /// and `source`), then delete `discovered` rows absent from the new set.
    /// Manual entries are always kept.
    pub async fn replace_discovered_models(
        &self,
        endpoint_id: &str,
        models: &[(String, Option<String>)],
    ) -> Result<(), DbError> {
        let endpoint_id = endpoint_id.to_string();
        let models = models.to_vec();
        self.call(move |conn| {
            let tx = conn.unchecked_transaction()?;
            let now = chrono::Utc::now().to_rfc3339();
            for (remote_id, raw_json) in &models {
                tx.execute(
                    &format!(
                        "INSERT INTO models ({MODEL_COLUMNS}) \
                         VALUES (?, ?, ?, 'discovered', 0, NULL, ?, ?) \
                         ON CONFLICT(endpoint_id, remote_model_id) DO UPDATE SET \
                             raw_json = excluded.raw_json, \
                             last_seen_at = excluded.last_seen_at"
                    ),
                    params![
                        uuid::Uuid::now_v7().to_string(),
                        endpoint_id,
                        remote_id,
                        raw_json,
                        now,
                    ],
                )?;
            }
            if models.is_empty() {
                tx.execute(
                    "DELETE FROM models WHERE endpoint_id = ? AND source = 'discovered'",
                    params![endpoint_id],
                )?;
            } else {
                let placeholders = vec!["?"; models.len()].join(", ");
                let sql = format!(
                    "DELETE FROM models WHERE endpoint_id = ? AND source = 'discovered' \
                     AND remote_model_id NOT IN ({placeholders})"
                );
                let mut stmt = tx.prepare(&sql)?;
                let mut params_vec: Vec<rusqlite::types::Value> =
                    Vec::with_capacity(models.len() + 1);
                params_vec.push(endpoint_id.clone().into());
                params_vec.extend(models.iter().map(|(id, _)| id.clone().into()));
                stmt.execute(rusqlite::params_from_iter(params_vec))?;
            }
            tx.commit()?;
            Ok(())
        })
        .await
    }

    pub async fn set_model_hidden(
        &self,
        endpoint_id: &str,
        remote_model_id: &str,
        hidden: bool,
    ) -> Result<Option<ModelRow>, DbError> {
        let (endpoint_id, remote_model_id) = (endpoint_id.to_string(), remote_model_id.to_string());
        self.call(move |conn| {
            let n = conn.execute(
                "UPDATE models SET hidden = ? WHERE endpoint_id = ? AND remote_model_id = ?",
                params![hidden, endpoint_id, remote_model_id],
            )?;
            if n == 0 {
                return Ok(None);
            }
            let mut stmt = conn.prepare(&format!(
                "SELECT {MODEL_COLUMNS} FROM models \
                 WHERE endpoint_id = ? AND remote_model_id = ?"
            ))?;
            let row = stmt.query_row(params![endpoint_id, remote_model_id], model_from_row)?;
            Ok(Some(row))
        })
        .await
    }

    pub async fn set_all_models_hidden(
        &self,
        endpoint_id: &str,
        hidden: bool,
    ) -> Result<Vec<ModelRow>, DbError> {
        let endpoint_id = endpoint_id.to_string();
        self.call(move |conn| {
            conn.execute(
                "UPDATE models SET hidden = ? WHERE endpoint_id = ?",
                params![hidden, endpoint_id],
            )?;
            let mut stmt = conn.prepare(&format!(
                "SELECT {MODEL_COLUMNS} FROM models WHERE endpoint_id = ? ORDER BY remote_model_id"
            ))?;
            let rows = stmt
                .query_map(params![endpoint_id], model_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }
}

impl Db {
    pub async fn insert_project(&self, project: &ProjectRow) -> Result<(), DbError> {
        let p = project.clone();
        self.call(move |conn| {
            conn.execute(
                &format!("INSERT INTO projects ({PROJECT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                params![
                    p.id,
                    p.name,
                    p.instructions,
                    p.endpoint_id,
                    p.model_id,
                    p.created_at,
                    p.updated_at
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_projects(&self) -> Result<Vec<ProjectRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {PROJECT_COLUMNS} FROM projects ORDER BY updated_at DESC, id"
            ))?;
            let rows = stmt
                .query_map([], project_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn get_project(&self, id: &str) -> Result<Option<ProjectRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            Ok(conn
                .query_row(
                    &format!("SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?"),
                    params![id],
                    project_from_row,
                )
                .optional()?)
        })
        .await
    }

    pub async fn update_project(&self, project: &ProjectRow) -> Result<bool, DbError> {
        let p = project.clone();
        self.call(move |conn| {
            Ok(conn.execute(
                "UPDATE projects SET name = ?, instructions = ?, endpoint_id = ?, model_id = ?, \
                 updated_at = ? WHERE id = ?",
                params![
                    p.name,
                    p.instructions,
                    p.endpoint_id,
                    p.model_id,
                    p.updated_at,
                    p.id
                ],
            )? > 0)
        })
        .await
    }

    pub async fn delete_project(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            Ok(conn.execute("DELETE FROM projects WHERE id = ?", params![id])? > 0)
        })
        .await
    }

    pub async fn insert_conversation(&self, conversation: &ConversationRow) -> Result<(), DbError> {
        let c = conversation.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO conversations ({CONVERSATION_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    c.id,
                    c.project_id,
                    c.title,
                    c.endpoint_id,
                    c.model_id,
                    c.archived_at,
                    c.created_at,
                    c.updated_at
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_conversations(
        &self,
        project_id: Option<&str>,
        archived: bool,
    ) -> Result<Vec<ConversationRow>, DbError> {
        let project_id = project_id.map(str::to_string);
        self.call(move |conn| {
            let archived_clause = if archived {
                "archived_at IS NOT NULL"
            } else {
                "archived_at IS NULL"
            };
            // Include a per-conversation message count so the client can detect
            // empty conversations (e.g. to reuse one instead of creating a new
            // one). Correlated subquery keeps it to a single round-trip.
            let columns_with_count =
                format!("{CONVERSATION_COLUMNS}, (SELECT COUNT(*) FROM messages WHERE conversation_id = conversations.id) AS message_count");
            let (sql, bind_project) = if project_id.is_some() {
                (
                    format!(
                        "SELECT {columns_with_count} FROM conversations \
                         WHERE project_id = ? AND {archived_clause} \
                         ORDER BY updated_at DESC, id"
                    ),
                    true,
                )
            } else {
                (
                    format!(
                        "SELECT {columns_with_count} FROM conversations \
                         WHERE {archived_clause} ORDER BY updated_at DESC, id"
                    ),
                    false,
                )
            };
            let mut stmt = conn.prepare(&sql)?;
            let rows = if bind_project {
                stmt.query_map(params![project_id], conversation_from_row)?
                    .collect::<Result<Vec<_>, _>>()?
            } else {
                stmt.query_map([], conversation_from_row)?
                    .collect::<Result<Vec<_>, _>>()?
            };
            Ok(rows)
        })
        .await
    }

    pub async fn get_conversation(&self, id: &str) -> Result<Option<ConversationRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            Ok(conn
                .query_row(
                    &format!("SELECT {CONVERSATION_COLUMNS} FROM conversations WHERE id = ?"),
                    params![id],
                    conversation_from_row,
                )
                .optional()?)
        })
        .await
    }

    pub async fn update_conversation(
        &self,
        conversation: &ConversationRow,
    ) -> Result<bool, DbError> {
        let c = conversation.clone();
        self.call(move |conn| {
            Ok(conn.execute(
                "UPDATE conversations SET project_id = ?, title = ?, endpoint_id = ?, \
                 model_id = ?, archived_at = ?, updated_at = ? WHERE id = ?",
                params![
                    c.project_id,
                    c.title,
                    c.endpoint_id,
                    c.model_id,
                    c.archived_at,
                    c.updated_at,
                    c.id
                ],
            )? > 0)
        })
        .await
    }

    pub async fn delete_conversation(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            Ok(conn.execute("DELETE FROM conversations WHERE id = ?", params![id])? > 0)
        })
        .await
    }

    pub async fn insert_message(&self, message: &MessageRow) -> Result<(), DbError> {
        let m = message.clone();
        self.call(move |conn| {
            let tx = conn.unchecked_transaction()?;
            tx.execute(
                &format!("INSERT INTO messages ({MESSAGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)"),
                params![
                    m.id,
                    m.conversation_id,
                    m.role,
                    m.content,
                    m.status,
                    m.created_at,
                    m.tool_events_json
                ],
            )?;
            tx.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                params![m.created_at, m.conversation_id],
            )?;
            tx.commit()?;
            Ok(())
        })
        .await
    }

    pub async fn list_messages(&self, conversation_id: &str) -> Result<Vec<MessageRow>, DbError> {
        let conversation_id = conversation_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {MESSAGE_COLUMNS} FROM messages WHERE conversation_id = ? \
                 ORDER BY created_at, id"
            ))?;
            let rows = stmt
                .query_map(params![conversation_id], message_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn insert_run(&self, run: &RunRow) -> Result<(), DbError> {
        let r = run.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO runs ({RUN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    r.id,
                    r.conversation_id,
                    r.user_message_id,
                    r.assistant_message_id,
                    r.status,
                    r.endpoint_id,
                    r.model_id,
                    r.started_at,
                    r.completed_at,
                    r.usage_json,
                    r.error_json
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn get_run(&self, id: &str) -> Result<Option<RunRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            Ok(conn
                .query_row(
                    &format!("SELECT {RUN_COLUMNS} FROM runs WHERE id = ?"),
                    params![id],
                    run_from_row,
                )
                .optional()?)
        })
        .await
    }

    pub async fn update_run(&self, run: &RunRow) -> Result<bool, DbError> {
        let r = run.clone();
        self.call(move |conn| {
            Ok(conn.execute(
                "UPDATE runs SET user_message_id = ?, assistant_message_id = ?, status = ?, \
                 endpoint_id = ?, model_id = ?, completed_at = ?, usage_json = ?, error_json = ? \
                 WHERE id = ?",
                params![
                    r.user_message_id,
                    r.assistant_message_id,
                    r.status,
                    r.endpoint_id,
                    r.model_id,
                    r.completed_at,
                    r.usage_json,
                    r.error_json,
                    r.id
                ],
            )? > 0)
        })
        .await
    }

    /// Recover runs left in-flight by a previous process. This is called once
    /// during host startup before new chat work is accepted.
    pub async fn interrupt_running_runs(&self) -> Result<usize, DbError> {
        let completed_at = chrono::Utc::now().to_rfc3339();
        self.call(move |conn| {
            Ok(conn.execute(
                "UPDATE runs SET status = 'interrupted', completed_at = ? WHERE status = 'running'",
                params![completed_at],
            )?)
        })
        .await
    }

    pub async fn search_conversations(&self, query: &str) -> Result<Vec<ConversationRow>, DbError> {
        let fts_query = query
            .split_whitespace()
            .filter(|part| !part.is_empty())
            .map(|part| format!("\"{}\"", part.replace('"', "\"\"")))
            .collect::<Vec<_>>()
            .join(" AND ");
        if fts_query.is_empty() {
            return Ok(Vec::new());
        }
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {} FROM conversation_search s \
                 JOIN conversations c ON c.id = s.conversation_id \
                 WHERE conversation_search MATCH ? \
                 ORDER BY bm25(conversation_search), c.updated_at DESC",
                CONVERSATION_COLUMNS
                    .split(", ")
                    .map(|column| format!("c.{column}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            ))?;
            let rows = stmt
                .query_map(params![fts_query], conversation_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn list_provider_models(
        &self,
        enabled: Option<bool>,
    ) -> Result<Vec<ProviderModelRow>, DbError> {
        self.call(move |conn| {
            let mut sql = "SELECT e.id, e.name, e.base_url, m.remote_model_id, m.hidden \
                           FROM models m JOIN endpoints e ON e.id = m.endpoint_id"
                .to_string();
            if let Some(enabled) = enabled {
                sql.push_str(if enabled {
                    " WHERE m.hidden = 0"
                } else {
                    " WHERE m.hidden = 1"
                });
            }
            sql.push_str(" ORDER BY e.name, m.remote_model_id");
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt
                .query_map([], |row| {
                    Ok(ProviderModelRow {
                        provider_id: row.get(0)?,
                        provider_name: row.get(1)?,
                        provider_url: row.get(2)?,
                        model_id: row.get(3)?,
                        enabled: !row.get::<_, bool>(4)?,
                    })
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn resolve_conversation_model(
        &self,
        conversation_id: &str,
    ) -> Result<ResolvedModel, ModelResolutionError> {
        let conversation = self
            .get_conversation(conversation_id)
            .await?
            .ok_or_else(|| {
                ModelResolutionError::ConversationNotFound(conversation_id.to_string())
            })?;
        let project = match conversation.project_id.as_deref() {
            Some(id) => self.get_project(id).await?,
            None => None,
        };

        let selection = if let Some(provider_id) = conversation.endpoint_id.clone() {
            Some((provider_id, conversation.model_id.clone()))
        } else if let Some(project) = &project {
            project
                .endpoint_id
                .clone()
                .map(|provider_id| (provider_id, project.model_id.clone()))
        } else {
            None
        };

        let (provider_id, explicit_model) = match selection {
            Some(selection) => selection,
            None => {
                // No conversation- or project-level model is set. Prefer an
                // endpoint that has a configured default model; otherwise fall
                // back to any endpoint with at least one enabled (non-hidden)
                // model so a freshly-created model-less conversation still runs
                // as long as the user has a usable model configured. Only fail
                // when there is genuinely no usable model anywhere.
                if let Some(endpoint) = self
                    .list_endpoints()
                    .await?
                    .into_iter()
                    .find(|endpoint| endpoint.default_model_id.is_some())
                {
                    (endpoint.id, None)
                } else {
                    self.list_provider_models(Some(true))
                        .await?
                        .into_iter()
                        .next()
                        .map(|model| (model.provider_id, Some(model.model_id)))
                        .ok_or_else(|| {
                            ModelResolutionError::NotConfigured(conversation_id.to_string())
                        })?
                }
            }
        };

        let provider = self
            .get_endpoint(&provider_id)
            .await?
            .ok_or_else(|| ModelResolutionError::ProviderNotFound(provider_id.clone()))?;
        let model_id = explicit_model
            .or_else(|| provider.default_model_id.clone())
            .ok_or_else(|| ModelResolutionError::ProviderDefaultMissing(provider_id.clone()))?;
        let model = self
            .get_model(&provider_id, &model_id)
            .await?
            .ok_or_else(|| ModelResolutionError::ModelNotFound {
                provider_id: provider_id.clone(),
                model_id: model_id.clone(),
            })?;
        if model.hidden {
            return Err(ModelResolutionError::ModelDisabled {
                provider_id,
                model_id,
            });
        }
        Ok(ResolvedModel {
            provider_id: provider.id,
            provider_name: provider.name,
            provider_url: provider.base_url,
            model_id,
            tls_verify: provider.tls_verify,
        })
    }

    pub async fn insert_knowledge_source(
        &self,
        source: &KnowledgeSourceRow,
        chunks: &[KnowledgeChunkRow],
    ) -> Result<(), DbError> {
        let source = source.clone();
        let chunks = chunks.to_vec();
        self.call(move |conn| {
            let tx = conn.unchecked_transaction()?;
            tx.execute(
                &format!(
                    "INSERT INTO knowledge_sources ({KNOWLEDGE_SOURCE_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    source.id,
                    source.title,
                    source.source_type,
                    source.source_uri,
                    source.content,
                    source.chunk_count,
                    source.created_at,
                    source.updated_at,
                    source.project_id,
                ],
            )?;
            for chunk in chunks {
                tx.execute(
                    "INSERT INTO knowledge_chunks \
                     (id, source_id, position, content, embedding_json, created_at) \
                     VALUES (?, ?, ?, ?, ?, ?)",
                    params![
                        chunk.id,
                        chunk.source_id,
                        chunk.position,
                        chunk.content,
                        chunk.embedding_json,
                        chunk.created_at,
                    ],
                )?;
            }
            tx.commit()?;
            Ok(())
        })
        .await
    }

    pub async fn list_knowledge_sources(
        &self,
        project_id: Option<&str>,
    ) -> Result<Vec<KnowledgeSourceRow>, DbError> {
        let project_id = project_id.map(str::to_owned);
        self.call(move |conn| {
            // Some(id) → that project's sources only.
            // None → global sources only (project_id IS NULL).
            // This preserves the existing app-wide Knowledge behavior for
            // callers that omit the project scope.
            let mut stmt = if project_id.is_some() {
                conn.prepare(&format!(
                    "SELECT {KNOWLEDGE_SOURCE_COLUMNS} FROM knowledge_sources \
                     WHERE project_id = ? ORDER BY created_at DESC, id DESC"
                ))?
            } else {
                conn.prepare(&format!(
                    "SELECT {KNOWLEDGE_SOURCE_COLUMNS} FROM knowledge_sources \
                     WHERE project_id IS NULL ORDER BY created_at DESC, id DESC"
                ))?
            };
            let rows = if let Some(id) = &project_id {
                stmt.query_map(params![id], knowledge_source_from_row)?
                    .collect::<Result<Vec<_>, _>>()?
            } else {
                stmt.query_map([], knowledge_source_from_row)?
                    .collect::<Result<Vec<_>, _>>()?
            };
            Ok(rows)
        })
        .await
    }

    pub async fn list_knowledge_chunks(
        &self,
        project_id: Option<&str>,
    ) -> Result<Vec<KnowledgeChunkRow>, DbError> {
        let project_id = project_id.map(str::to_owned);
        self.call(move |conn| {
            // For a project: its own sources plus global (project_id IS NULL).
            // For global/ungrouped: global sources only.
            let mut stmt = if project_id.is_some() {
                conn.prepare(
                    "SELECT c.id, c.source_id, s.title, c.position, c.content, \
                            c.embedding_json, c.created_at \
                     FROM knowledge_chunks c \
                     JOIN knowledge_sources s ON s.id = c.source_id \
                     WHERE s.project_id IS NULL OR s.project_id = ? \
                     ORDER BY s.created_at DESC, c.position",
                )?
            } else {
                conn.prepare(
                    "SELECT c.id, c.source_id, s.title, c.position, c.content, \
                            c.embedding_json, c.created_at \
                     FROM knowledge_chunks c \
                     JOIN knowledge_sources s ON s.id = c.source_id \
                     WHERE s.project_id IS NULL \
                     ORDER BY s.created_at DESC, c.position",
                )?
            };
            let rows = if let Some(id) = &project_id {
                stmt.query_map(params![id], knowledge_chunk_from_row)?
                    .collect::<Result<Vec<_>, _>>()?
            } else {
                stmt.query_map([], knowledge_chunk_from_row)?
                    .collect::<Result<Vec<_>, _>>()?
            };
            Ok(rows)
        })
        .await
    }

    pub async fn delete_knowledge_source(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            Ok(conn.execute("DELETE FROM knowledge_sources WHERE id = ?", params![id])? > 0)
        })
        .await
    }

    pub async fn set_tool_enabled(&self, tool_id: &str, enabled: bool) -> Result<(), DbError> {
        let tool_id = tool_id.to_string();
        let updated_at = chrono::Utc::now().to_rfc3339();
        self.call(move |conn| {
            conn.execute(
                "INSERT INTO tool_settings (tool_id, enabled, updated_at) VALUES (?, ?, ?) \
                 ON CONFLICT(tool_id) DO UPDATE SET enabled = excluded.enabled, \
                 updated_at = excluded.updated_at",
                params![tool_id, enabled, updated_at],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn tool_enabled(
        &self,
        tool_id: &str,
        default_enabled: bool,
    ) -> Result<bool, DbError> {
        let tool_id = tool_id.to_string();
        self.call(move |conn| {
            Ok(conn
                .query_row(
                    "SELECT enabled FROM tool_settings WHERE tool_id = ?",
                    params![tool_id],
                    |row| row.get(0),
                )
                .optional()?
                .unwrap_or(default_enabled))
        })
        .await
    }

    pub async fn analytics_runs(&self) -> Result<Vec<AnalyticsRunRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(
                "SELECT COALESCE(e.name, 'Unknown provider'), \
                        COALESCE(r.model_id, 'Unknown model'), r.status, \
                        r.started_at, r.completed_at, r.usage_json \
                 FROM runs r LEFT JOIN endpoints e ON e.id = r.endpoint_id \
                 ORDER BY r.started_at DESC",
            )?;
            let rows = stmt
                .query_map([], |row| {
                    Ok(AnalyticsRunRow {
                        provider_name: row.get(0)?,
                        model_id: row.get(1)?,
                        status: row.get(2)?,
                        started_at: row.get(3)?,
                        completed_at: row.get(4)?,
                        usage_json: row.get(5)?,
                    })
                })?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    // ---- browser (ADR-0009) ----

    pub async fn list_browser_profiles(&self) -> Result<Vec<BrowserProfileRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_PROFILE_COLUMNS} FROM browser_profiles ORDER BY created_at"
            ))?;
            let rows = stmt
                .query_map([], browser_profile_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn get_browser_profile(
        &self,
        id: &str,
    ) -> Result<Option<BrowserProfileRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_PROFILE_COLUMNS} FROM browser_profiles WHERE id = ?"
            ))?;
            let mut rows = stmt.query_map(params![id], browser_profile_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    pub async fn insert_browser_profile(&self, profile: &BrowserProfileRow) -> Result<(), DbError> {
        let p = profile.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO browser_profiles ({BROWSER_PROFILE_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    p.id,
                    p.name,
                    p.engine,
                    p.executable_path,
                    p.profile_path,
                    p.created_at,
                    p.updated_at,
                    p.last_used_at,
                ],
            )?;
            // Every profile gets a default preferences row.
            conn.execute(
                "INSERT OR IGNORE INTO browser_preferences (profile_id) VALUES (?)",
                params![p.id],
            )?;
            Ok(())
        })
        .await
    }

    /// Update mutable profile fields (name, engine, executable_path,
    /// profile_path) and bump `updated_at`.
    pub async fn update_browser_profile(
        &self,
        profile: &BrowserProfileRow,
    ) -> Result<bool, DbError> {
        let p = profile.clone();
        self.call(move |conn| {
            let n = conn.execute(
                "UPDATE browser_profiles SET name = ?, engine = ?, executable_path = ?, \
                 profile_path = ?, updated_at = ?, last_used_at = ? WHERE id = ?",
                params![
                    p.name,
                    p.engine,
                    p.executable_path,
                    p.profile_path,
                    p.updated_at,
                    p.last_used_at,
                    p.id,
                ],
            )?;
            Ok(n > 0)
        })
        .await
    }

    pub async fn touch_browser_profile(&self, id: &str, used_at: &str) -> Result<(), DbError> {
        let (id, used_at) = (id.to_string(), used_at.to_string());
        self.call(move |conn| {
            conn.execute(
                "UPDATE browser_profiles SET last_used_at = ? WHERE id = ?",
                params![used_at, id],
            )?;
            Ok(())
        })
        .await
    }

    /// Delete a profile; preferences cascade (foreign keys on). The `default`
    /// profile is protected at the handler layer, not here.
    pub async fn delete_browser_profile(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let n = conn.execute("DELETE FROM browser_profiles WHERE id = ?", params![id])?;
            Ok(n > 0)
        })
        .await
    }

    pub async fn get_browser_preferences(
        &self,
        profile_id: &str,
    ) -> Result<Option<BrowserPreferencesRow>, DbError> {
        let profile_id = profile_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_PREFERENCES_COLUMNS} FROM browser_preferences \
                 WHERE profile_id = ?"
            ))?;
            let mut rows = stmt.query_map(params![profile_id], browser_preferences_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    pub async fn upsert_browser_preferences(
        &self,
        prefs: &BrowserPreferencesRow,
    ) -> Result<(), DbError> {
        let p = prefs.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO browser_preferences ({BROWSER_PREFERENCES_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
                     ON CONFLICT(profile_id) DO UPDATE SET \
                         panel_mode = excluded.panel_mode, \
                         panel_width = excluded.panel_width, \
                         previous_panel_width = excluded.previous_panel_width, \
                         auto_open_on_tool_call = excluded.auto_open_on_tool_call, \
                         keep_running_when_hidden = excluded.keep_running_when_hidden, \
                         remote_streaming_enabled = excluded.remote_streaming_enabled, \
                         model_mode = excluded.model_mode, \
                         model_endpoint_id = excluded.model_endpoint_id, \
                         model_id = excluded.model_id"
                ),
                params![
                    p.profile_id,
                    p.panel_mode,
                    p.panel_width,
                    p.previous_panel_width,
                    p.auto_open_on_tool_call,
                    p.keep_running_when_hidden,
                    p.remote_streaming_enabled,
                    p.model_mode,
                    p.model_endpoint_id,
                    p.model_id,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn insert_browser_task(&self, task: &BrowserTaskRow) -> Result<(), DbError> {
        let t = task.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO browser_tasks ({BROWSER_TASK_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    t.id,
                    t.profile_id,
                    t.conversation_id,
                    t.run_id,
                    t.tool_call_id,
                    t.task_text,
                    t.initial_url,
                    t.final_url,
                    t.status,
                    t.result_text,
                    t.error_code,
                    t.error_message,
                    t.started_at,
                    t.finished_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn get_browser_task(&self, id: &str) -> Result<Option<BrowserTaskRow>, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_TASK_COLUMNS} FROM browser_tasks WHERE id = ?"
            ))?;
            let mut rows = stmt.query_map(params![id], browser_task_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    /// The currently active task for a profile, if any. Active statuses are
    /// the "one active task per profile" set from spec §19.
    pub async fn active_browser_task(
        &self,
        profile_id: &str,
    ) -> Result<Option<BrowserTaskRow>, DbError> {
        let profile_id = profile_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_TASK_COLUMNS} FROM browser_tasks \
                 WHERE profile_id = ? AND status IN \
                 ('awaiting_approval', 'starting', 'running', 'paused_for_user', 'stopping') \
                 ORDER BY started_at DESC LIMIT 1"
            ))?;
            let mut rows = stmt.query_map(params![profile_id], browser_task_from_row)?;
            Ok(rows.next().transpose()?)
        })
        .await
    }

    pub async fn update_browser_task_status(&self, id: &str, status: &str) -> Result<(), DbError> {
        let (id, status) = (id.to_string(), status.to_string());
        self.call(move |conn| {
            conn.execute(
                "UPDATE browser_tasks SET status = ? WHERE id = ?",
                params![status, id],
            )?;
            Ok(())
        })
        .await
    }

    /// Mark a task finished with its terminal status, result/error and
    /// finished_at timestamp.
    #[allow(clippy::too_many_arguments)]
    pub async fn finish_browser_task(
        &self,
        id: &str,
        status: &str,
        result_text: Option<&str>,
        error_code: Option<&str>,
        error_message: Option<&str>,
        final_url: Option<&str>,
        finished_at: &str,
    ) -> Result<(), DbError> {
        let owned = (
            id.to_string(),
            status.to_string(),
            result_text.map(str::to_string),
            error_code.map(str::to_string),
            error_message.map(str::to_string),
            final_url.map(str::to_string),
            finished_at.to_string(),
        );
        self.call(move |conn| {
            let (id, status, result_text, error_code, error_message, final_url, finished_at) =
                owned;
            conn.execute(
                "UPDATE browser_tasks SET status = ?, result_text = ?, error_code = ?, \
                 error_message = ?, final_url = COALESCE(?, final_url), finished_at = ? \
                 WHERE id = ?",
                params![
                    status,
                    result_text,
                    error_code,
                    error_message,
                    final_url,
                    finished_at,
                    id
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_browser_tasks(
        &self,
        profile_id: &str,
        limit: i64,
    ) -> Result<Vec<BrowserTaskRow>, DbError> {
        let profile_id = profile_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_TASK_COLUMNS} FROM browser_tasks \
                 WHERE profile_id = ? ORDER BY started_at DESC LIMIT ?"
            ))?;
            let rows = stmt
                .query_map(params![profile_id, limit], browser_task_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn insert_browser_permission(
        &self,
        permission: &BrowserPermissionRow,
    ) -> Result<(), DbError> {
        let p = permission.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO browser_permissions ({BROWSER_PERMISSION_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    p.id,
                    p.profile_id,
                    p.origin,
                    p.capability,
                    p.scope,
                    p.conversation_id,
                    p.expires_at,
                    p.created_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_browser_permissions(
        &self,
        profile_id: &str,
    ) -> Result<Vec<BrowserPermissionRow>, DbError> {
        let profile_id = profile_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_PERMISSION_COLUMNS} FROM browser_permissions \
                 WHERE profile_id = ? ORDER BY created_at"
            ))?;
            let rows = stmt
                .query_map(params![profile_id], browser_permission_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    /// Stored grants matching a capability check. `origin` matches either the
    /// exact origin or a NULL (any-origin) grant; `conversation_id` matches
    /// either the exact conversation or a NULL grant.
    pub async fn find_browser_permissions(
        &self,
        profile_id: &str,
        capability: &str,
        origin: Option<&str>,
        conversation_id: Option<&str>,
    ) -> Result<Vec<BrowserPermissionRow>, DbError> {
        let owned = (
            profile_id.to_string(),
            capability.to_string(),
            origin.map(str::to_string),
            conversation_id.map(str::to_string),
        );
        self.call(move |conn| {
            let (profile_id, capability, origin, conversation_id) = owned;
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_PERMISSION_COLUMNS} FROM browser_permissions \
                 WHERE profile_id = ? AND capability = ? \
                   AND (origin IS NULL OR origin = ?) \
                   AND (conversation_id IS NULL OR conversation_id = ?) \
                   AND (expires_at IS NULL OR expires_at > ?) \
                 ORDER BY created_at"
            ))?;
            let now = chrono::Utc::now().to_rfc3339();
            let rows = stmt
                .query_map(
                    params![
                        profile_id,
                        capability,
                        origin.unwrap_or_default(),
                        conversation_id.unwrap_or_default(),
                        now
                    ],
                    browser_permission_from_row,
                )?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn delete_browser_permission(&self, id: &str) -> Result<bool, DbError> {
        let id = id.to_string();
        self.call(move |conn| {
            let n = conn.execute("DELETE FROM browser_permissions WHERE id = ?", params![id])?;
            Ok(n > 0)
        })
        .await
    }

    pub async fn insert_browser_download(
        &self,
        download: &BrowserDownloadRow,
    ) -> Result<(), DbError> {
        let d = download.clone();
        self.call(move |conn| {
            conn.execute(
                &format!(
                    "INSERT INTO browser_downloads ({BROWSER_DOWNLOAD_COLUMNS}) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                params![
                    d.id,
                    d.profile_id,
                    d.task_id,
                    d.source_url,
                    d.filename,
                    d.local_path,
                    d.mime_type,
                    d.size_bytes,
                    d.status,
                    d.created_at,
                ],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn update_browser_download(
        &self,
        id: &str,
        status: &str,
        size_bytes: Option<i64>,
    ) -> Result<(), DbError> {
        let (id, status) = (id.to_string(), status.to_string());
        self.call(move |conn| {
            conn.execute(
                "UPDATE browser_downloads SET status = ?, size_bytes = COALESCE(?, size_bytes) \
                 WHERE id = ?",
                params![status, size_bytes, id],
            )?;
            Ok(())
        })
        .await
    }

    pub async fn list_browser_downloads(
        &self,
        profile_id: &str,
        limit: i64,
    ) -> Result<Vec<BrowserDownloadRow>, DbError> {
        let profile_id = profile_id.to_string();
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {BROWSER_DOWNLOAD_COLUMNS} FROM browser_downloads \
                 WHERE profile_id = ? ORDER BY created_at DESC LIMIT ?"
            ))?;
            let rows = stmt
                .query_map(params![profile_id, limit], browser_download_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDb {
        db: Option<Db>,
        dir: PathBuf,
    }

    impl TestDb {
        fn new() -> Self {
            let dir =
                std::env::temp_dir().join(format!("agentgpt-db-test-{}", uuid::Uuid::now_v7()));
            let db = Db::open(&dir.join("agentgpt.sqlite3")).expect("open db");
            Self { db: Some(db), dir }
        }

        fn db(&self) -> Db {
            self.db.as_ref().expect("db").clone()
        }
    }

    impl Drop for TestDb {
        fn drop(&mut self) {
            // Close the connection before removing the tempdir (Windows file
            // locking). Clones made by the test are already dropped by then.
            drop(self.db.take());
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn sample_endpoint(id: &str) -> EndpointRow {
        EndpointRow {
            id: id.to_string(),
            name: "Test".to_string(),
            base_url: "http://127.0.0.1:1234".to_string(),
            timeout_seconds: 15,
            tls_verify: true,
            has_api_key: false,
            default_model_id: None,
            last_test_status: None,
            last_tested_at: None,
            created_at: "2026-07-20T00:00:00Z".to_string(),
            updated_at: "2026-07-20T00:00:00Z".to_string(),
        }
    }

    fn sample_project(id: &str) -> ProjectRow {
        ProjectRow {
            id: id.to_string(),
            name: "Project".to_string(),
            instructions: "Use project rules".to_string(),
            endpoint_id: None,
            model_id: None,
            created_at: "2026-07-20T00:00:00Z".to_string(),
            updated_at: "2026-07-20T00:00:00Z".to_string(),
        }
    }

    fn sample_conversation(id: &str, project_id: Option<&str>) -> ConversationRow {
        ConversationRow {
            id: id.to_string(),
            project_id: project_id.map(str::to_string),
            title: "Native chat".to_string(),
            endpoint_id: None,
            model_id: None,
            archived_at: None,
            created_at: "2026-07-20T00:00:00Z".to_string(),
            updated_at: "2026-07-20T00:00:00Z".to_string(),
            message_count: None,
        }
    }

    #[test]
    fn migrations_are_idempotent() {
        let dir = std::env::temp_dir().join(format!("agentgpt-db-mig-{}", uuid::Uuid::now_v7()));
        let path = dir.join("agentgpt.sqlite3");
        let db = Db::open(&path).expect("first open");
        drop(db);
        // Second open re-runs the migration runner against an existing DB.
        let db = Db::open(&path).expect("second open");
        let conn = lock(&db.conn);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_migrations", [], |r| r.get(0))
            .expect("count");
        assert_eq!(count as usize, MIGRATIONS.len());
        drop(conn);
        drop(db);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn endpoint_crud() {
        let t = TestDb::new();
        let db = t.db();
        db.insert_endpoint(&sample_endpoint("ep-1")).await.unwrap();
        db.insert_endpoint(&sample_endpoint("ep-2")).await.unwrap();

        let got = db.get_endpoint("ep-1").await.unwrap().expect("exists");
        assert_eq!(got.name, "Test");
        assert!(got.tls_verify);
        assert_eq!(db.list_endpoints().await.unwrap().len(), 2);

        let mut updated = got.clone();
        updated.name = "Renamed".to_string();
        updated.default_model_id = Some("qwen3:8b".to_string());
        db.update_endpoint(&updated).await.unwrap();
        let got = db.get_endpoint("ep-1").await.unwrap().unwrap();
        assert_eq!(got.name, "Renamed");
        assert_eq!(got.default_model_id.as_deref(), Some("qwen3:8b"));

        db.update_test_status("ep-1", "ok", "2026-07-20T01:00:00Z")
            .await
            .unwrap();
        let got = db.get_endpoint("ep-1").await.unwrap().unwrap();
        assert_eq!(got.last_test_status.as_deref(), Some("ok"));

        assert!(db.delete_endpoint("ep-2").await.unwrap());
        assert!(!db.delete_endpoint("ep-2").await.unwrap());
        assert_eq!(db.list_endpoints().await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn model_upsert_preserves_hidden_and_source() {
        let t = TestDb::new();
        let db = t.db();
        db.insert_endpoint(&sample_endpoint("ep-1")).await.unwrap();

        db.upsert_model("ep-1", "m-1", "discovered", None)
            .await
            .unwrap();
        db.set_model_hidden("ep-1", "m-1", true).await.unwrap();
        // Refresh upsert must not clobber hidden.
        db.upsert_model("ep-1", "m-1", "discovered", Some("{}"))
            .await
            .unwrap();
        let row = db.get_model("ep-1", "m-1").await.unwrap().unwrap();
        assert!(row.hidden);
        assert_eq!(row.raw_json.as_deref(), Some("{}"));

        // Manual entry keeps source='manual' when later "discovered".
        db.upsert_model("ep-1", "m-2", "manual", None)
            .await
            .unwrap();
        db.upsert_model("ep-1", "m-2", "discovered", None)
            .await
            .unwrap();
        let row = db.get_model("ep-1", "m-2").await.unwrap().unwrap();
        assert_eq!(row.source, "manual");
    }

    #[tokio::test]
    async fn set_all_models_hidden_bulk() {
        let t = TestDb::new();
        let db = t.db();
        db.insert_endpoint(&sample_endpoint("ep-1")).await.unwrap();
        db.upsert_model("ep-1", "m-1", "discovered", None)
            .await
            .unwrap();
        db.upsert_model("ep-1", "m-2", "manual", None)
            .await
            .unwrap();

        let rows = db.set_all_models_hidden("ep-1", true).await.unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|r| r.hidden));

        let rows = db.set_all_models_hidden("ep-1", false).await.unwrap();
        assert!(rows.iter().all(|r| !r.hidden));
        // Manual source survives the bulk toggle.
        let manual = rows.iter().find(|r| r.remote_model_id == "m-2").unwrap();
        assert_eq!(manual.source, "manual");
    }

    #[tokio::test]
    async fn replace_discovered_keeps_manual_and_removes_stale() {
        let t = TestDb::new();
        let db = t.db();
        db.insert_endpoint(&sample_endpoint("ep-1")).await.unwrap();
        db.upsert_model("ep-1", "keep", "discovered", None)
            .await
            .unwrap();
        db.upsert_model("ep-1", "stale", "discovered", None)
            .await
            .unwrap();
        db.upsert_model("ep-1", "manual", "manual", None)
            .await
            .unwrap();

        db.replace_discovered_models("ep-1", &[("keep".to_string(), None)])
            .await
            .unwrap();
        let rows = db.list_models("ep-1").await.unwrap();
        let ids: Vec<&str> = rows.iter().map(|r| r.remote_model_id.as_str()).collect();
        assert_eq!(ids, ["keep", "manual"]);
    }

    #[tokio::test]
    async fn endpoint_delete_cascades_models() {
        let t = TestDb::new();
        let db = t.db();
        db.insert_endpoint(&sample_endpoint("ep-1")).await.unwrap();
        db.upsert_model("ep-1", "m-1", "discovered", None)
            .await
            .unwrap();
        assert!(db.delete_endpoint("ep-1").await.unwrap());
        assert!(db.list_models("ep-1").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn phase3_crud_search_archive_and_cascades() {
        let t = TestDb::new();
        let db = t.db();
        db.insert_project(&sample_project("p-1")).await.unwrap();
        db.insert_conversation(&sample_conversation("c-1", Some("p-1")))
            .await
            .unwrap();
        db.insert_conversation(&sample_conversation("c-2", None))
            .await
            .unwrap();

        let message = MessageRow {
            id: "m-1".to_string(),
            conversation_id: "c-1".to_string(),
            role: "user".to_string(),
            content: "Discuss sqlite persistence".to_string(),
            status: "completed".to_string(),
            created_at: "2026-07-20T00:01:00Z".to_string(),
            tool_events_json: None,
        };
        db.insert_message(&message).await.unwrap();
        let run = RunRow {
            id: "r-1".to_string(),
            conversation_id: "c-1".to_string(),
            user_message_id: Some("m-1".to_string()),
            assistant_message_id: None,
            status: "running".to_string(),
            endpoint_id: None,
            model_id: None,
            started_at: "2026-07-20T00:01:00Z".to_string(),
            completed_at: None,
            usage_json: None,
            error_json: None,
        };
        db.insert_run(&run).await.unwrap();

        assert_eq!(db.list_projects().await.unwrap().len(), 1);
        assert_eq!(
            db.list_conversations(Some("p-1"), false)
                .await
                .unwrap()
                .len(),
            1
        );
        assert_eq!(db.list_messages("c-1").await.unwrap(), vec![message]);
        assert_eq!(
            db.search_conversations("sqlite").await.unwrap()[0].id,
            "c-1"
        );
        assert_eq!(db.search_conversations("Native").await.unwrap().len(), 2);

        let mut archived = db.get_conversation("c-1").await.unwrap().unwrap();
        archived.archived_at = Some("2026-07-20T00:02:00Z".to_string());
        db.update_conversation(&archived).await.unwrap();
        assert!(db
            .list_conversations(Some("p-1"), false)
            .await
            .unwrap()
            .is_empty());
        assert_eq!(
            db.list_conversations(Some("p-1"), true)
                .await
                .unwrap()
                .len(),
            1
        );

        assert!(db.delete_project("p-1").await.unwrap());
        assert_eq!(
            db.get_conversation("c-1")
                .await
                .unwrap()
                .unwrap()
                .project_id,
            None
        );
        assert!(db.delete_conversation("c-1").await.unwrap());
        assert!(db.list_messages("c-1").await.unwrap().is_empty());
        assert!(db.get_run("r-1").await.unwrap().is_none());
        assert!(db.search_conversations("sqlite").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn model_resolution_obeys_override_chain_and_rejects_disabled() {
        let t = TestDb::new();
        let db = t.db();
        let mut endpoint = sample_endpoint("ep-1");
        endpoint.default_model_id = Some("provider-default".to_string());
        db.insert_endpoint(&endpoint).await.unwrap();
        for model in ["provider-default", "project-model", "chat-model"] {
            db.upsert_model("ep-1", model, "discovered", None)
                .await
                .unwrap();
        }

        let mut project = sample_project("p-1");
        project.endpoint_id = Some("ep-1".to_string());
        project.model_id = Some("project-model".to_string());
        db.insert_project(&project).await.unwrap();
        db.insert_conversation(&sample_conversation("c-1", Some("p-1")))
            .await
            .unwrap();
        db.insert_conversation(&sample_conversation("c-2", None))
            .await
            .unwrap();

        assert_eq!(
            db.resolve_conversation_model("c-1").await.unwrap().model_id,
            "project-model"
        );
        assert_eq!(
            db.resolve_conversation_model("c-2").await.unwrap().model_id,
            "provider-default"
        );

        let mut conversation = db.get_conversation("c-1").await.unwrap().unwrap();
        conversation.endpoint_id = Some("ep-1".to_string());
        conversation.model_id = Some("chat-model".to_string());
        db.update_conversation(&conversation).await.unwrap();
        assert_eq!(
            db.resolve_conversation_model("c-1").await.unwrap().model_id,
            "chat-model"
        );

        db.set_model_hidden("ep-1", "chat-model", true)
            .await
            .unwrap();
        assert!(matches!(
            db.resolve_conversation_model("c-1").await,
            Err(ModelResolutionError::ModelDisabled { .. })
        ));
        let enabled = db.list_provider_models(Some(true)).await.unwrap();
        assert_eq!(enabled.len(), 2);
        assert!(enabled.iter().all(|model| model.enabled));
    }

    /// A model-less conversation should resolve via any endpoint that has an
    /// enabled model, even when no endpoint has a `default_model_id` set.
    #[tokio::test]
    async fn model_resolution_falls_back_to_any_enabled_model() {
        let t = TestDb::new();
        let db = t.db();
        // No default_model_id on either endpoint — the old logic would reject
        // a model-less conversation with NotConfigured.
        let endpoint = sample_endpoint("ep-1");
        db.insert_endpoint(&endpoint).await.unwrap();
        for model in ["some-model", "other-model"] {
            db.upsert_model("ep-1", model, "discovered", None)
                .await
                .unwrap();
        }
        db.insert_conversation(&sample_conversation("c-3", None))
            .await
            .unwrap();

        let resolved = db.resolve_conversation_model("c-3").await.unwrap();
        assert_eq!(resolved.provider_id, "ep-1");
        assert!(
            resolved.model_id == "some-model" || resolved.model_id == "other-model",
            "should resolve to one of the enabled models, got {}",
            resolved.model_id
        );

        // Hiding all models on the only endpoint leaves nothing usable.
        db.set_model_hidden("ep-1", "some-model", true)
            .await
            .unwrap();
        db.set_model_hidden("ep-1", "other-model", true)
            .await
            .unwrap();
        assert!(matches!(
            db.resolve_conversation_model("c-3").await,
            Err(ModelResolutionError::NotConfigured(_))
        ));
    }

    #[tokio::test]
    async fn phase3_data_survives_reopen_and_runs_update() {
        let dir = std::env::temp_dir().join(format!("agentgpt-db-reopen-{}", uuid::Uuid::now_v7()));
        let path = dir.join("agentgpt.sqlite3");
        let db = Db::open(&path).unwrap();
        db.insert_project(&sample_project("p-1")).await.unwrap();
        db.insert_conversation(&sample_conversation("c-1", Some("p-1")))
            .await
            .unwrap();
        let mut run = RunRow {
            id: "r-1".to_string(),
            conversation_id: "c-1".to_string(),
            user_message_id: None,
            assistant_message_id: None,
            status: "running".to_string(),
            endpoint_id: None,
            model_id: None,
            started_at: "2026-07-20T00:00:00Z".to_string(),
            completed_at: None,
            usage_json: None,
            error_json: None,
        };
        db.insert_run(&run).await.unwrap();
        run.status = "completed".to_string();
        run.completed_at = Some("2026-07-20T00:01:00Z".to_string());
        run.usage_json = Some("{\"tokens\":12}".to_string());
        assert!(db.update_run(&run).await.unwrap());
        let mut abandoned = run.clone();
        abandoned.id = "r-2".to_string();
        abandoned.status = "running".to_string();
        abandoned.completed_at = None;
        abandoned.usage_json = None;
        db.insert_run(&abandoned).await.unwrap();
        drop(db);

        let reopened = Db::open(&path).unwrap();
        assert_eq!(reopened.list_projects().await.unwrap().len(), 1);
        assert_eq!(
            reopened
                .get_conversation("c-1")
                .await
                .unwrap()
                .unwrap()
                .title,
            "Native chat"
        );
        assert_eq!(
            reopened.get_run("r-1").await.unwrap().unwrap().status,
            "completed"
        );
        assert_eq!(reopened.interrupt_running_runs().await.unwrap(), 1);
        let recovered = reopened.get_run("r-2").await.unwrap().unwrap();
        assert_eq!(recovered.status, "interrupted");
        assert!(recovered.completed_at.is_some());
        assert_eq!(reopened.interrupt_running_runs().await.unwrap(), 0);
        drop(reopened);
        let _ = std::fs::remove_dir_all(dir);
    }
}
