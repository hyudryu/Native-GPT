//! Generate `app-data/mcp_servers.json` from the `remote_hosts` table.
//!
//! The agent-runtime sidecar loads this file and connects to each configured
//! bridge as an MCP client (Strands `MCPClient`, streamable-http). The file
//! follows the `mcpServers` shape `MCPClient.load_servers` expects, plus one
//! non-standard `tls_verify` key consumed by our own loader (ignored, with a
//! warning, by Strands' generic loader). Bearer tokens come from the keychain
//! (`host:<id>`); hosts without a token are skipped. See the bridge MCP
//! design spec (`docs/superpowers/specs/2026-07-22-bridge-mcp-server-design.md`).
//!
//! Regenerated at startup and after every remote-hosts mutation so the file
//! always reflects the current table.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use crate::state::SharedState;

/// Location of the MCP servers config. Honors `AGENTGPT_DATA_DIR` (same
/// precedence as the DB/assets paths) and falls back to
/// `<repo_root>/app-data/mcp_servers.json`.
pub fn servers_path(repo_root: &Path) -> PathBuf {
    if let Ok(dir) = std::env::var("AGENTGPT_DATA_DIR") {
        if !dir.trim().is_empty() {
            return PathBuf::from(dir).join("mcp_servers.json");
        }
    }
    repo_root.join("app-data").join("mcp_servers.json")
}

/// Rebuild `mcp_servers.json` from the current `remote_hosts` table.
///
/// One entry per host that has a keychain token: name
/// `agentgpt-bridge-<shortid>`, url `<base_url>/mcp`, streamable-http
/// transport, bearer auth header, and `tls_verify: false` when the host row
/// disables verification. Returns the written path.
pub async fn regenerate(state: &SharedState) -> anyhow::Result<PathBuf> {
    let hosts = state.db.list_remote_hosts().await?;
    let mut servers = Map::new();
    for host in &hosts {
        if !host.has_token {
            continue;
        }
        let Some(token) = state
            .secrets
            .get(&crate::remote_hosts::secret_key(&host.id))
        else {
            continue;
        };
        let shortid: String = host.id.chars().take(8).collect();
        let name = format!("agentgpt-bridge-{shortid}");
        let url = format!("{}/mcp", host.base_url.trim_end_matches('/'));
        let mut entry = json!({
            "url": url,
            "transport": "streamable-http",
            "headers": { "Authorization": format!("Bearer {token}") },
        });
        if !host.tls_verify {
            entry
                .as_object_mut()
                .expect("entry is an object")
                .insert("tls_verify".to_string(), Value::Bool(false));
        }
        servers.insert(name, entry);
    }

    let path = servers_path(&state.repo_root);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let doc = json!({ "mcpServers": Value::Object(servers) });
    let body = serde_json::to_string_pretty(&doc)?;
    // Write-then-rename so a crash mid-write never leaves a torn config.
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, format!("{body}\n"))?;
    std::fs::rename(&tmp, &path)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::RemoteHostRow;
    use crate::secrets::KeyStore;

    fn host_row(id: &str, base_url: &str, tls_verify: bool, has_token: bool) -> RemoteHostRow {
        let now = chrono::Utc::now().to_rfc3339();
        RemoteHostRow {
            id: id.to_string(),
            name: format!("host-{id}"),
            base_url: base_url.to_string(),
            tls_verify,
            has_token,
            status: None,
            last_checked_at: None,
            workloads_json: None,
            created_at: now.clone(),
            updated_at: now,
        }
    }

    #[tokio::test]
    async fn regenerate_writes_one_entry_per_tokened_host() {
        let rig = crate::state::test_state("tok");
        let state = rig.state.clone();

        // Host with a token and TLS verification disabled.
        let a = host_row(
            "11111111-2222-3333-4444-555555555555",
            "https://gx10:8443/",
            false,
            true,
        );
        rig.secrets
            .set(&crate::remote_hosts::secret_key(&a.id), "secret-a")
            .unwrap();
        state.db.insert_remote_host(&a).await.unwrap();

        // Host without a token: skipped.
        let b = host_row(
            "66666666-7777-8888-9999-000000000000",
            "http://bridge:8443",
            true,
            false,
        );
        state.db.insert_remote_host(&b).await.unwrap();

        let path = regenerate(&state).await.unwrap();
        assert_eq!(path, servers_path(&state.repo_root));
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let servers = doc["mcpServers"].as_object().unwrap();
        assert_eq!(servers.len(), 1);

        let entry = &servers["agentgpt-bridge-11111111"];
        assert_eq!(entry["url"], json!("https://gx10:8443/mcp"));
        assert_eq!(entry["transport"], json!("streamable-http"));
        assert_eq!(entry["headers"]["Authorization"], json!("Bearer secret-a"));
        assert_eq!(entry["tls_verify"], json!(false));
    }

    #[tokio::test]
    async fn regenerate_omits_tls_verify_when_enabled() {
        let rig = crate::state::test_state("tok");
        let state = rig.state.clone();
        let row = host_row(
            "abcdef01-2222-3333-4444-555555555555",
            "http://bridge:8443",
            true,
            true,
        );
        rig.secrets
            .set(&crate::remote_hosts::secret_key(&row.id), "t")
            .unwrap();
        state.db.insert_remote_host(&row).await.unwrap();

        let path = regenerate(&state).await.unwrap();
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let entry = &doc["mcpServers"]["agentgpt-bridge-abcdef01"];
        assert!(entry.get("tls_verify").is_none());
    }

    #[tokio::test]
    async fn regenerate_with_no_hosts_writes_empty_mapping() {
        let rig = crate::state::test_state("tok");
        let path = regenerate(&rig.state).await.unwrap();
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(doc["mcpServers"], json!({}));
    }
}
