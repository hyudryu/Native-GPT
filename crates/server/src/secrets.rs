//! Keychain abstraction (`KeyStore`) so REST handlers never touch the OS
//! keychain directly and tests can substitute an in-memory implementation.
//! Raw API keys flow only: client -> keychain -> sidecar NDJSON payload.
//! They are never logged and never returned by the REST API.

use agentgpt_secure_store::{SecureStore, StoreError};

/// Secret storage for endpoint API keys (key = endpoint id).
pub trait KeyStore: Send + Sync {
    fn get(&self, key: &str) -> Option<String>;
    fn set(&self, key: &str, value: &str) -> Result<(), StoreError>;
    fn delete(&self, key: &str) -> Result<(), StoreError>;
}

impl KeyStore for SecureStore {
    fn get(&self, key: &str) -> Option<String> {
        SecureStore::get(self, key)
    }

    fn set(&self, key: &str, value: &str) -> Result<(), StoreError> {
        SecureStore::set(self, key, value)
    }

    fn delete(&self, key: &str) -> Result<(), StoreError> {
        SecureStore::delete(self, key)
    }
}

/// In-memory `KeyStore` for tests.
#[cfg(test)]
#[derive(Default)]
pub struct MemoryKeyStore {
    map: std::sync::Mutex<std::collections::HashMap<String, String>>,
}

#[cfg(test)]
impl MemoryKeyStore {
    pub fn new() -> Self {
        Self::default()
    }
}

#[cfg(test)]
impl KeyStore for MemoryKeyStore {
    fn get(&self, key: &str) -> Option<String> {
        self.map.lock().unwrap().get(key).cloned()
    }

    fn set(&self, key: &str, value: &str) -> Result<(), StoreError> {
        self.map
            .lock()
            .unwrap()
            .insert(key.to_string(), value.to_string());
        Ok(())
    }

    fn delete(&self, key: &str) -> Result<(), StoreError> {
        self.map.lock().unwrap().remove(key);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_keystore_round_trip() {
        let store = MemoryKeyStore::new();
        assert_eq!(store.get("k"), None);
        store.set("k", "v").unwrap();
        assert_eq!(store.get("k").as_deref(), Some("v"));
        store.delete("k").unwrap();
        assert_eq!(store.get("k"), None);
    }
}
