//! Browser permission engine (spec §11): capability/scope grants backed by
//! the `browser_permissions` table, private-network URL policy, and file-path
//! approval against approved roots.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::path::{Component, Path, PathBuf};

use crate::db::{BrowserPermissionRow, Db, DbError};

use super::protocol::{PermissionCapability, PermissionScope};

pub enum PermissionDecision {
    Allow,
    NeedApproval,
}

/// Ported from `knowledge.rs::unsafe_ip` (SSRF guard): private, loopback,
/// link-local, multicast, unspecified, and cloud-metadata addresses.
pub fn unsafe_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            ip.is_private()
                || ip.is_loopback()
                || ip.is_link_local()
                || ip.is_multicast()
                || ip.is_unspecified()
                || ip == Ipv4Addr::new(169, 254, 169, 254)
        }
        IpAddr::V6(ip) => {
            ip.is_loopback()
                || ip.is_unspecified()
                || ip.is_multicast()
                || is_ipv6_unique_local(ip)
                || is_ipv6_link_local(ip)
        }
    }
}

fn is_ipv6_unique_local(ip: Ipv6Addr) -> bool {
    ip.segments()[0] & 0xfe00 == 0xfc00
}

fn is_ipv6_link_local(ip: Ipv6Addr) -> bool {
    ip.segments()[0] & 0xffc0 == 0xfe80
}

/// Classification of a navigation target (spec §11.3/§11.4).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UrlClass {
    /// Ordinary public http(s) page.
    Public,
    /// localhost / private / link-local target.
    PrivateNetwork,
    /// Native GPT's own HTTP server — always blocked for the browser.
    OwnServer,
    /// file://, chrome://, chrome-extension://, data:, about:blank excepted.
    BlockedScheme,
    /// Not parseable as a URL.
    Invalid,
}

/// Classify `url`. `server_port` is the host's own HTTP port; any loopback
/// URL targeting it is [`UrlClass::OwnServer`] (spec §11.4: internal API
/// routes are always blocked from browser automation).
pub fn classify_url(url: &str, server_port: u16) -> UrlClass {
    let trimmed = url.trim();
    if trimmed.eq_ignore_ascii_case("about:blank") {
        return UrlClass::PrivateNetwork;
    }
    let Some(scheme_end) = trimmed.find("://") else {
        return UrlClass::Invalid;
    };
    let scheme = trimmed[..scheme_end].to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return UrlClass::BlockedScheme;
    }
    let rest = &trimmed[scheme_end + 3..];
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    // Strip optional userinfo (never expected; reject credentials in URLs).
    if authority.contains('@') {
        return UrlClass::BlockedScheme;
    }
    let (host, port) = if let Some(rest) = authority.strip_prefix('[') {
        // Bracketed IPv6: "[::1]:8080" or "[::1]".
        match rest.find(']') {
            Some(end) => {
                let host = &rest[..end];
                let port = rest[end + 1..]
                    .strip_prefix(':')
                    .and_then(|p| p.parse::<u16>().ok());
                (host, port)
            }
            None => (authority, None),
        }
    } else {
        match authority.rsplit_once(':') {
            Some((host, port)) => (host, port.parse::<u16>().ok()),
            None => (authority, None),
        }
    };
    let port = port.unwrap_or(if scheme == "https" { 443 } else { 80 });
    let host_lower = host.to_ascii_lowercase();
    let ip = host_lower
        .parse::<IpAddr>()
        .ok()
        .or(match host_lower.as_str() {
            "localhost" => Some(IpAddr::V4(Ipv4Addr::LOCALHOST)),
            _ => None,
        });
    if let Some(ip) = ip {
        if ip.is_loopback() && port == server_port {
            return UrlClass::OwnServer;
        }
        if unsafe_ip(ip) {
            return UrlClass::PrivateNetwork;
        }
        return UrlClass::Public;
    }
    // Unresolvable-literal hostnames (e.g. intranet DNS names) are treated as
    // public here; DNS resolution happens in Chromium itself. Known-local
    // suffixes are conservative catches.
    if host_lower.ends_with(".local")
        || host_lower.ends_with(".internal")
        || host_lower.ends_with(".localhost")
    {
        return UrlClass::PrivateNetwork;
    }
    UrlClass::Public
}

/// Navigation decision for the policy in spec §11.4.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NavigationDecision {
    Allow,
    /// Agent-driven navigation to a private-network origin needs a
    /// `navigate_private_network` grant.
    NeedApproval,
    /// Always blocked: own internal routes, non-web schemes, invalid URLs.
    Blocked,
}

pub fn navigation_decision(
    url: &str,
    agent_initiated: bool,
    server_port: u16,
) -> NavigationDecision {
    match classify_url(url, server_port) {
        UrlClass::Public => NavigationDecision::Allow,
        UrlClass::PrivateNetwork => {
            if agent_initiated {
                NavigationDecision::NeedApproval
            } else {
                NavigationDecision::Allow
            }
        }
        UrlClass::OwnServer | UrlClass::BlockedScheme | UrlClass::Invalid => {
            NavigationDecision::Blocked
        }
    }
}

/// Check stored grants for a capability (spec §11.1). Any unexpired matching
/// grant allows; everything else needs approval.
pub async fn check(
    db: &Db,
    profile_id: &str,
    capability: PermissionCapability,
    origin: Option<&str>,
    conversation_id: Option<&str>,
) -> Result<PermissionDecision, DbError> {
    let grants = db
        .find_browser_permissions(profile_id, capability.as_str(), origin, conversation_id)
        .await?;
    // A conversation-scoped grant only counts for that conversation; origin
    // and profile scopes apply broadly. `once`/`task` grants are recorded
    // for audit but consumed by the task runner, so they do not allow here.
    let allows = grants.iter().any(|grant| {
        matches!(grant.scope.as_str(), "origin" | "profile")
            || (grant.scope == "conversation"
                && conversation_id.is_some()
                && grant.conversation_id.as_deref() == conversation_id)
    });
    Ok(if allows {
        PermissionDecision::Allow
    } else {
        PermissionDecision::NeedApproval
    })
}

/// Record a grant after user approval.
pub async fn grant(
    db: &Db,
    profile_id: &str,
    capability: PermissionCapability,
    scope: PermissionScope,
    origin: Option<&str>,
    conversation_id: Option<&str>,
) -> Result<BrowserPermissionRow, DbError> {
    let row = BrowserPermissionRow {
        id: uuid::Uuid::now_v7().to_string(),
        profile_id: profile_id.to_string(),
        origin: origin.map(str::to_string),
        capability: capability.as_str().to_string(),
        scope: scope.as_str().to_string(),
        conversation_id: conversation_id.map(str::to_string),
        expires_at: None,
        created_at: chrono::Utc::now().to_rfc3339(),
    };
    db.insert_browser_permission(&row).await?;
    Ok(row)
}

/// Server-side mirror of the tools allowed-roots idea (spec §6.5): upload
/// files must live under an approved root — conversation attachments,
/// project files, or explicitly approved paths.
#[derive(Debug, Clone, Default)]
pub struct ApprovedRoots {
    roots: Vec<PathBuf>,
}

impl ApprovedRoots {
    pub fn new(roots: Vec<PathBuf>) -> Self {
        Self { roots }
    }

    pub fn add(&mut self, root: PathBuf) {
        self.roots.push(root);
    }

    pub fn roots(&self) -> &[PathBuf] {
        &self.roots
    }

    /// Lexical normalization: resolve `.`/`..` without touching the
    /// filesystem (canonicalize requires the path to exist and symlinks make
    /// comparisons brittle; the CDP call re-checks existence separately).
    fn normalize(path: &Path) -> PathBuf {
        let mut out = PathBuf::new();
        for component in path.components() {
            match component {
                Component::CurDir => {}
                Component::ParentDir => {
                    out.pop();
                }
                other => out.push(other.as_os_str()),
            }
        }
        out
    }

    /// True when `path` exists and lives under one of the approved roots.
    pub fn is_allowed(&self, path: &Path) -> bool {
        if !path.is_file() {
            return false;
        }
        let normalized = Self::normalize(path);
        self.roots.iter().any(|root| {
            let root = Self::normalize(root);
            normalized.starts_with(&root)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SERVER_PORT: u16 = 9123;

    #[test]
    fn classifies_url_targets() {
        assert_eq!(
            classify_url("https://example.com/page", SERVER_PORT),
            UrlClass::Public
        );
        assert_eq!(
            classify_url("http://192.168.1.10:3000/app", SERVER_PORT),
            UrlClass::PrivateNetwork
        );
        assert_eq!(
            classify_url("http://localhost:3000", SERVER_PORT),
            UrlClass::PrivateNetwork
        );
        assert_eq!(
            classify_url(
                "http://127.0.0.1:9123/internal/browser/command",
                SERVER_PORT
            ),
            UrlClass::OwnServer
        );
        assert_eq!(
            classify_url("http://[::1]:9123/api/health", SERVER_PORT),
            UrlClass::OwnServer
        );
        assert_eq!(
            classify_url("file:///etc/passwd", SERVER_PORT),
            UrlClass::BlockedScheme
        );
        assert_eq!(
            classify_url("chrome://settings", SERVER_PORT),
            UrlClass::BlockedScheme
        );
        assert_eq!(
            classify_url("https://user:pass@example.com/", SERVER_PORT),
            UrlClass::BlockedScheme
        );
        assert_eq!(classify_url("not a url", SERVER_PORT), UrlClass::Invalid);
        assert_eq!(
            classify_url("http://printer.local/", SERVER_PORT),
            UrlClass::PrivateNetwork
        );
    }

    #[test]
    fn navigation_policy_matches_spec() {
        // Manual navigation to private networks is allowed; agent-driven
        // needs approval; own server and blocked schemes always blocked.
        assert_eq!(
            navigation_decision("http://localhost:3000", false, SERVER_PORT),
            NavigationDecision::Allow
        );
        assert_eq!(
            navigation_decision("http://localhost:3000", true, SERVER_PORT),
            NavigationDecision::NeedApproval
        );
        assert_eq!(
            navigation_decision("https://example.com", true, SERVER_PORT),
            NavigationDecision::Allow
        );
        assert_eq!(
            navigation_decision("http://127.0.0.1:9123/x", false, SERVER_PORT),
            NavigationDecision::Blocked
        );
        assert_eq!(
            navigation_decision("file:///c:/windows", false, SERVER_PORT),
            NavigationDecision::Blocked
        );
    }

    #[test]
    fn unsafe_ip_covers_metadata_and_v6() {
        assert!(unsafe_ip("169.254.169.254".parse().unwrap()));
        assert!(unsafe_ip("10.0.0.1".parse().unwrap()));
        assert!(unsafe_ip("127.0.0.1".parse().unwrap()));
        assert!(unsafe_ip("::1".parse().unwrap()));
        assert!(unsafe_ip("fe80::1".parse().unwrap()));
        assert!(unsafe_ip("fd00::1".parse().unwrap()));
        assert!(!unsafe_ip("8.8.8.8".parse().unwrap()));
        assert!(!unsafe_ip("2606:4700:4700::1111".parse().unwrap()));
    }

    #[test]
    fn approved_roots_enforce_containment() {
        let root =
            std::env::temp_dir().join(format!("agentgpt-roots-test-{}", uuid::Uuid::now_v7()));
        let allowed_dir = root.join("attachments");
        std::fs::create_dir_all(&allowed_dir).unwrap();
        let inside = allowed_dir.join("resume.pdf");
        std::fs::write(&inside, b"pdf").unwrap();
        let outside = root.join("secret.txt");
        std::fs::write(&outside, b"nope").unwrap();

        let roots = ApprovedRoots::new(vec![allowed_dir.clone()]);
        assert!(roots.is_allowed(&inside));
        // Traversal that lands back inside is fine.
        assert!(roots.is_allowed(
            &allowed_dir
                .join("..")
                .join("attachments")
                .join("resume.pdf")
        ));
        // Outside the root is not.
        assert!(!roots.is_allowed(&outside));
        assert!(!roots.is_allowed(&allowed_dir.join("..").join("secret.txt")));
        // Nonexistent paths are not uploadable.
        assert!(!roots.is_allowed(&allowed_dir.join("missing.pdf")));
        let _ = std::fs::remove_dir_all(&root);
    }
}
