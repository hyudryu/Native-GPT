//! Host-originated broadcast events (M3 multi-client sync).
//!
//! Mutations that write SQLite emit a `data.changed` envelope on
//! [`crate::state::AppState::host_events`]; `ws.rs` forwards it to every
//! connected client so other devices can refresh. Payloads stay small and
//! carry identifiers only — never message content.

use serde_json::Value;

use crate::protocol::Envelope;
use crate::state::SharedState;

/// Emit a `data.changed` envelope. No subscribers is fine (single client).
pub fn data_changed(state: &SharedState, payload: Value) {
    let env = Envelope::new("data.changed", payload);
    // Ignore SendError (no active receivers).
    let _ = state.host_events.send(env);
}
