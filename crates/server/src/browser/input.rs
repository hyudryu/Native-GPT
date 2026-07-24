//! UI input → CDP input translation (spec §10.2). Panel coordinates are CSS
//! pixels; CDP expects device pixels, so the device scale factor is applied.
//! Manual input is blocked while a Page Agent task owns the tab — the manager
//! checks `manual_control_enabled` before calling these.

use serde_json::{json, Value};
use tracing::debug;

use super::protocol::{KeyInput, MouseButton, MouseEventKind, MouseInput, WheelInput};

/// CDP modifier bitmask values.
pub const MODIFIER_ALT: u32 = 1;
pub const MODIFIER_CTRL: u32 = 2;
pub const MODIFIER_META: u32 = 4;
pub const MODIFIER_SHIFT: u32 = 8;

/// Build a CDP modifier bitmask from individual flags.
pub fn modifiers_mask(alt: bool, ctrl: bool, meta: bool, shift: bool) -> u32 {
    let mut mask = 0;
    if alt {
        mask |= MODIFIER_ALT;
    }
    if ctrl {
        mask |= MODIFIER_CTRL;
    }
    if meta {
        mask |= MODIFIER_META;
    }
    if shift {
        mask |= MODIFIER_SHIFT;
    }
    mask
}

/// Panel (CSS px) → viewport (device px) coordinates for high-DPI displays.
///
/// `device_scale_factor` originates from client viewport messages, so it is
/// not trusted: a zero (or negative / non-finite) factor would silently
/// collapse every coordinate to `(0.0, 0.0)` and send input to the top-left
/// corner. Such values are clamped back to `1.0` (identity) instead of
/// panicking, since panicking here would let a client kill a handler task.
pub fn to_viewport_coords(x: f64, y: f64, device_scale_factor: f64) -> (f64, f64) {
    let dsf = if device_scale_factor.is_finite() && device_scale_factor > 0.0 {
        device_scale_factor
    } else {
        debug!(
            value = device_scale_factor,
            "invalid device_scale_factor, using 1.0"
        );
        1.0
    };
    (x * dsf, y * dsf)
}

fn cdp_button(button: MouseButton) -> &'static str {
    match button {
        MouseButton::None => "none",
        MouseButton::Left => "left",
        MouseButton::Middle => "middle",
        MouseButton::Right => "right",
    }
}

/// `Input.dispatchMouseEvent` params for move/down/up.
pub fn mouse_params(input: &MouseInput, device_scale_factor: f64) -> Value {
    let (x, y) = to_viewport_coords(input.x, input.y, device_scale_factor);
    let kind = match input.kind {
        MouseEventKind::Move => "mouseMoved",
        MouseEventKind::Down => "mousePressed",
        MouseEventKind::Up => "mouseReleased",
    };
    json!({
        "type": kind,
        "x": x,
        "y": y,
        "button": cdp_button(input.button),
        "clickCount": input.click_count,
        "modifiers": input.modifiers,
    })
}

/// `Input.dispatchMouseEvent` params for wheel scrolling.
pub fn wheel_params(input: &WheelInput, device_scale_factor: f64) -> Value {
    let (x, y) = to_viewport_coords(input.x, input.y, device_scale_factor);
    json!({
        "type": "mouseWheel",
        "x": x,
        "y": y,
        "deltaX": input.delta_x,
        "deltaY": input.delta_y,
        "modifiers": input.modifiers,
    })
}

/// `Input.dispatchKeyEvent` params. `kind` is one of `rawKeyDown`, `keyDown`,
/// `keyUp`, `char` (validated; defaults to `rawKeyDown` for unknown values).
pub fn key_params(input: &KeyInput) -> Value {
    let kind = match input.kind.as_str() {
        "rawKeyDown" | "keyDown" | "keyUp" | "char" => input.kind.as_str(),
        other => {
            debug!(
                kind = other,
                "unknown KeyInput kind, defaulting to rawKeyDown"
            );
            "rawKeyDown"
        }
    };
    let mut params = json!({
        "type": kind,
        "modifiers": input.modifiers,
    });
    if !input.key.is_empty() {
        params["key"] = json!(input.key);
    }
    if !input.code.is_empty() {
        params["code"] = json!(input.code);
    }
    if !input.text.is_empty() {
        params["text"] = json!(input.text);
    }
    if input.windows_virtual_key_code != 0 {
        params["windowsVirtualKeyCode"] = json!(input.windows_virtual_key_code);
    }
    params
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modifiers_bitmask_matches_cdp_convention() {
        assert_eq!(modifiers_mask(false, false, false, false), 0);
        assert_eq!(modifiers_mask(true, false, false, false), 1);
        assert_eq!(modifiers_mask(false, true, false, false), 2);
        assert_eq!(modifiers_mask(false, false, true, false), 4);
        assert_eq!(modifiers_mask(false, false, false, true), 8);
        assert_eq!(modifiers_mask(true, true, true, true), 15);
    }

    #[test]
    fn coords_scale_for_high_dpi() {
        let (x, y) = to_viewport_coords(10.0, 20.0, 2.0);
        assert!((x - 20.0).abs() < f64::EPSILON);
        assert!((y - 40.0).abs() < f64::EPSILON);
    }

    #[test]
    fn invalid_scale_factor_falls_back_to_identity() {
        for bad in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            let (x, y) = to_viewport_coords(10.0, 20.0, bad);
            assert!((x - 10.0).abs() < f64::EPSILON, "dsf {bad}");
            assert!((y - 20.0).abs() < f64::EPSILON, "dsf {bad}");
        }
    }

    #[test]
    fn mouse_params_map_kinds_and_buttons() {
        let input = MouseInput {
            kind: MouseEventKind::Down,
            x: 5.0,
            y: 6.0,
            button: MouseButton::Right,
            click_count: 2,
            modifiers: modifiers_mask(false, true, false, true),
        };
        let params = mouse_params(&input, 1.5);
        assert_eq!(params["type"], "mousePressed");
        assert_eq!(params["button"], "right");
        assert_eq!(params["clickCount"], 2);
        assert_eq!(params["modifiers"], 10);
        assert!((params["x"].as_f64().unwrap() - 7.5).abs() < f64::EPSILON);
        assert!((params["y"].as_f64().unwrap() - 9.0).abs() < f64::EPSILON);
    }

    #[test]
    fn wheel_params_emit_mouse_wheel() {
        let input = WheelInput {
            x: 1.0,
            y: 2.0,
            delta_x: 0.0,
            delta_y: -120.0,
            modifiers: 0,
        };
        let params = wheel_params(&input, 1.0);
        assert_eq!(params["type"], "mouseWheel");
        assert_eq!(params["deltaY"], -120.0);
    }

    #[test]
    fn key_params_validate_kind_and_omit_empty_fields() {
        let input = KeyInput {
            kind: "bogus".into(),
            key: "Enter".into(),
            code: "Enter".into(),
            text: String::new(),
            windows_virtual_key_code: 13,
            modifiers: 0,
        };
        let params = key_params(&input);
        assert_eq!(params["type"], "rawKeyDown");
        assert_eq!(params["key"], "Enter");
        assert_eq!(params["windowsVirtualKeyCode"], 13);
        assert!(params.get("text").is_none());
    }
}
