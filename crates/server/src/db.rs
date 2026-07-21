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
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct MessageRow {
    pub id: String,
    pub conversation_id: String,
    pub role: String,
    pub content: String,
    pub status: String,
    pub created_at: String,
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
const MESSAGE_COLUMNS: &str = "id, conversation_id, role, content, status, created_at";
const RUN_COLUMNS: &str = "id, conversation_id, user_message_id, assistant_message_id, status, \
    endpoint_id, model_id, started_at, completed_at, usage_json, error_json";
const KNOWLEDGE_SOURCE_COLUMNS: &str =
    "id, title, source_type, source_uri, content, chunk_count, created_at, updated_at";

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
            let (sql, bind_project) = if project_id.is_some() {
                (
                    format!(
                        "SELECT {CONVERSATION_COLUMNS} FROM conversations \
                         WHERE project_id = ? AND {archived_clause} \
                         ORDER BY updated_at DESC, id"
                    ),
                    true,
                )
            } else {
                (
                    format!(
                        "SELECT {CONVERSATION_COLUMNS} FROM conversations \
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
                &format!("INSERT INTO messages ({MESSAGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)"),
                params![
                    m.id,
                    m.conversation_id,
                    m.role,
                    m.content,
                    m.status,
                    m.created_at
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
            None => self
                .list_endpoints()
                .await?
                .into_iter()
                .find(|endpoint| endpoint.default_model_id.is_some())
                .map(|endpoint| (endpoint.id, None))
                .ok_or_else(|| ModelResolutionError::NotConfigured(conversation_id.to_string()))?,
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
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
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

    pub async fn list_knowledge_sources(&self) -> Result<Vec<KnowledgeSourceRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {KNOWLEDGE_SOURCE_COLUMNS} FROM knowledge_sources \
                 ORDER BY created_at DESC, id DESC"
            ))?;
            let rows = stmt
                .query_map([], knowledge_source_from_row)?
                .collect::<Result<Vec<_>, _>>()?;
            Ok(rows)
        })
        .await
    }

    pub async fn list_knowledge_chunks(&self) -> Result<Vec<KnowledgeChunkRow>, DbError> {
        self.call(move |conn| {
            let mut stmt = conn.prepare(
                "SELECT c.id, c.source_id, s.title, c.position, c.content, \
                        c.embedding_json, c.created_at \
                 FROM knowledge_chunks c \
                 JOIN knowledge_sources s ON s.id = c.source_id \
                 ORDER BY s.created_at DESC, c.position",
            )?;
            let rows = stmt
                .query_map([], |row| {
                    Ok(KnowledgeChunkRow {
                        id: row.get(0)?,
                        source_id: row.get(1)?,
                        source_title: row.get(2)?,
                        position: row.get(3)?,
                        content: row.get(4)?,
                        embedding_json: row.get(5)?,
                        created_at: row.get(6)?,
                    })
                })?
                .collect::<Result<Vec<_>, _>>()?;
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
