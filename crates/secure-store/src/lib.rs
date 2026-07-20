//! Thin wrapper over the OS keychain (`keyring`) with an environment
//! variable fallback when no keychain is available (e.g. headless Linux).
//!
//! Phase 0 stub: the API is stable, real persistence (token storage,
//! rotation) is wired up in a later phase.

use keyring::Entry;

/// Errors from keychain write/delete operations.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("keychain error: {0}")]
    Keychain(String),
}

/// Keychain-backed secret store scoped to a service name.
pub struct SecureStore {
    service: String,
}

impl SecureStore {
    pub fn new(service: impl Into<String>) -> Self {
        Self {
            service: service.into(),
        }
    }

    /// Read a secret. Falls back to `AGENTGPT_SECRET_<KEY>` (uppercased,
    /// non-alphanumeric replaced with `_`) when the keychain is unavailable.
    pub fn get(&self, key: &str) -> Option<String> {
        match Entry::new(&self.service, key).and_then(|e| e.get_password()) {
            Ok(value) => Some(value),
            Err(keyring::Error::NoEntry) => None,
            Err(e) => {
                tracing::warn!("keychain unavailable ({e}); falling back to environment variable");
                std::env::var(env_fallback_key(key)).ok()
            }
        }
    }

    /// Store a secret in the keychain.
    pub fn set(&self, key: &str, value: &str) -> Result<(), StoreError> {
        Entry::new(&self.service, key)
            .and_then(|e| e.set_password(value))
            .map_err(|e| {
                tracing::warn!("failed to store secret in keychain: {e}");
                StoreError::Keychain(e.to_string())
            })
    }

    /// Delete a secret. Missing entries are not an error.
    pub fn delete(&self, key: &str) -> Result<(), StoreError> {
        match Entry::new(&self.service, key).and_then(|e| e.delete_credential()) {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => {
                tracing::warn!("failed to delete secret from keychain: {e}");
                Err(StoreError::Keychain(e.to_string()))
            }
        }
    }
}

/// Environment variable used as fallback for `key`.
pub fn env_fallback_key(key: &str) -> String {
    let sanitized: String = key
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '_'
            }
        })
        .collect();
    format!("AGENTGPT_SECRET_{sanitized}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fallback_key_is_uppercased_and_sanitized() {
        assert_eq!(env_fallback_key("auth-token"), "AGENTGPT_SECRET_AUTH_TOKEN");
        assert_eq!(env_fallback_key("a.b/c"), "AGENTGPT_SECRET_A_B_C");
    }

    #[test]
    fn missing_keychain_entry_yields_none_or_fallback() {
        // On CI there is typically no keychain; either path must not panic.
        let store = SecureStore::new("com.agentgpt.test");
        let _ = store.get("definitely-missing-key");
    }
}
