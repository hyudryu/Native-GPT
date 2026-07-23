import type {
  KeyInput,
  MouseButtonName,
  MouseInput,
  TextInput,
  WheelInput,
} from "./types";

/**
 * Translates DOM pointer/keyboard events on the viewport into browser stream
 * input commands (spec §10.2). CDP modifier bitmask: Alt=1 Ctrl=2 Meta=4
 * Shift=8 — matches `crates/server/src/browser/input.rs`.
 */

export const MODIFIER_ALT = 1;
export const MODIFIER_CTRL = 2;
export const MODIFIER_META = 4;
export const MODIFIER_SHIFT = 8;

export interface ModifierKeys {
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

export function modifiersMask(event: ModifierKeys): number {
  let mask = 0;
  if (event.altKey) mask |= MODIFIER_ALT;
  if (event.ctrlKey) mask |= MODIFIER_CTRL;
  if (event.metaKey) mask |= MODIFIER_META;
  if (event.shiftKey) mask |= MODIFIER_SHIFT;
  return mask;
}

export function mouseButtonName(button: number): MouseButtonName {
  switch (button) {
    case 0:
      return "left";
    case 1:
      return "middle";
    case 2:
      return "right";
    default:
      return "none";
  }
}

/** Windows virtual-key codes for common non-printable keys (CDP convention). */
const VK_BY_KEY: Record<string, number> = {
  Backspace: 8,
  Tab: 9,
  Enter: 13,
  Shift: 16,
  Control: 17,
  Alt: 18,
  Pause: 19,
  CapsLock: 20,
  Escape: 27,
  " ": 32,
  PageUp: 33,
  PageDown: 34,
  End: 35,
  Home: 36,
  ArrowLeft: 37,
  ArrowUp: 38,
  ArrowRight: 39,
  ArrowDown: 40,
  PrintScreen: 44,
  Insert: 45,
  Delete: 46,
  Meta: 91,
  ContextMenu: 93,
  NumLock: 144,
  ScrollLock: 145,
};

for (let i = 1; i <= 12; i += 1) {
  VK_BY_KEY[`F${i}`] = 111 + i;
}

/** Best-effort Windows virtual-key code for a KeyboardEvent. */
export function windowsVirtualKeyCode(event: { key: string }): number {
  const mapped = VK_BY_KEY[event.key];
  if (mapped !== undefined) return mapped;
  if (event.key.length === 1) return event.key.toUpperCase().charCodeAt(0);
  return 0;
}

const MODIFIER_CODES = new Set([
  "ShiftLeft",
  "ShiftRight",
  "ControlLeft",
  "ControlRight",
  "AltLeft",
  "AltRight",
  "MetaLeft",
  "MetaRight",
]);

function keyPayload(kind: string, event: KeyboardEvent): KeyInput {
  return {
    kind,
    key: event.key,
    code: event.code,
    windowsVirtualKeyCode: windowsVirtualKeyCode(event),
    modifiers: modifiersMask(event),
  };
}

export interface InputBridgeHandlers {
  sendMouse: (payload: MouseInput) => void;
  sendWheel: (payload: WheelInput) => void;
  sendKey: (payload: KeyInput) => void;
  sendText: (payload: TextInput) => void;
  /** Ctrl/Cmd+L focuses the address field and is never forwarded. */
  onFocusAddress: () => void;
  /** True while the agent owns the tab and manual input is blocked. */
  isBlocked: () => boolean;
  /**
   * Scale factors from rendered pixels to viewport CSS pixels. When the
   * screencast viewport matches the rendered element this is 1:1; during
   * resizes it corrects transient frame/panel size mismatches.
   */
  coordScale: () => { x: number; y: number };
}

/**
 * Attach native listeners translating events on `surface` (the element that
 * displays the frame) into stream commands. `focusTarget` is the hidden
 * textarea used for keyboard + IME/composition input. Returns a detach fn.
 */
export function attachInputBridge(
  surface: HTMLElement,
  focusTarget: HTMLTextAreaElement,
  handlers: InputBridgeHandlers,
): () => void {
  const pressedModifiers = new Set<string>();
  let pendingMove: MouseInput | null = null;
  let moveFrame = 0;

  const coords = (event: MouseEvent): { x: number; y: number } => {
    const rect = surface.getBoundingClientRect();
    const scale = handlers.coordScale();
    return {
      x: (event.clientX - rect.left) * scale.x,
      y: (event.clientY - rect.top) * scale.y,
    };
  };

  const flushMove = () => {
    moveFrame = 0;
    if (pendingMove && !handlers.isBlocked()) handlers.sendMouse(pendingMove);
    pendingMove = null;
  };

  const onPointerDown = (event: PointerEvent) => {
    focusTarget.focus({ preventScroll: true });
    if (handlers.isBlocked()) return;
    surface.setPointerCapture?.(event.pointerId);
    const { x, y } = coords(event);
    handlers.sendMouse({
      kind: "down",
      x,
      y,
      button: mouseButtonName(event.button),
      clickCount: event.detail || 1,
      modifiers: modifiersMask(event),
    });
  };

  const onPointerUp = (event: PointerEvent) => {
    if (handlers.isBlocked()) return;
    const { x, y } = coords(event);
    handlers.sendMouse({
      kind: "up",
      x,
      y,
      button: mouseButtonName(event.button),
      clickCount: event.detail || 1,
      modifiers: modifiersMask(event),
    });
  };

  const onPointerMove = (event: PointerEvent) => {
    const { x, y } = coords(event);
    // Coalesce high-frequency moves to one per animation frame.
    pendingMove = {
      kind: "move",
      x,
      y,
      button: "none",
      clickCount: 0,
      modifiers: modifiersMask(event),
    };
    if (moveFrame === 0) moveFrame = requestAnimationFrame(flushMove);
  };

  const onContextMenu = (event: MouseEvent) => {
    event.preventDefault();
    if (handlers.isBlocked()) return;
    const { x, y } = coords(event);
    const modifiers = modifiersMask(event);
    handlers.sendMouse({
      kind: "down",
      x,
      y,
      button: "right",
      clickCount: 1,
      modifiers,
    });
    handlers.sendMouse({
      kind: "up",
      x,
      y,
      button: "right",
      clickCount: 1,
      modifiers,
    });
  };

  const onWheel = (event: WheelEvent) => {
    event.preventDefault();
    if (handlers.isBlocked()) return;
    const { x, y } = coords(event);
    // deltaMode: 0 = pixels, 1 = lines, 2 = pages.
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
    handlers.sendWheel({
      x,
      y,
      deltaX: event.deltaX * unit,
      deltaY: event.deltaY * unit,
      modifiers: modifiersMask(event),
    });
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (
      (event.ctrlKey || event.metaKey) &&
      event.key.toLowerCase() === "l"
    ) {
      event.preventDefault();
      handlers.onFocusAddress();
      return;
    }
    if (MODIFIER_CODES.has(event.code)) pressedModifiers.add(event.code);
    if (handlers.isBlocked()) {
      event.preventDefault();
      return;
    }
    // Non-printable keys are forwarded as rawKeyDown and prevented locally;
    // printable text flows through beforeinput/composition instead (IME).
    if (event.key.length > 1 || event.ctrlKey || event.metaKey || event.altKey) {
      handlers.sendKey(keyPayload("rawKeyDown", event));
      if (event.key !== "Shift" && event.key !== "Control" && event.key !== "Alt" && event.key !== "Meta") {
        event.preventDefault();
      }
    }
  };

  const onKeyUp = (event: KeyboardEvent) => {
    pressedModifiers.delete(event.code);
    if (handlers.isBlocked()) return;
    handlers.sendKey(keyPayload("keyUp", event));
  };

  const onBeforeInput = (event: InputEvent) => {
    if (handlers.isBlocked()) {
      event.preventDefault();
      return;
    }
    if (event.inputType === "insertText" && !event.isComposing && event.data) {
      event.preventDefault();
      handlers.sendText({ text: event.data });
    }
  };

  const onCompositionStart = () => {
    // Composition text is committed on compositionend (IME support, spec §10.2).
  };

  const onCompositionEnd = (event: CompositionEvent) => {
    if (handlers.isBlocked()) {
      focusTarget.value = "";
      return;
    }
    if (event.data) handlers.sendText({ text: event.data });
    focusTarget.value = "";
  };

  /** Release stuck modifier keys on blur/disconnect (spec §10.2). */
  const onWindowBlur = () => {
    for (const code of pressedModifiers) {
      const key =
        code.replace("Left", "").replace("Right", "") || code;
      handlers.sendKey({
        kind: "keyUp",
        key,
        code,
        windowsVirtualKeyCode: windowsVirtualKeyCode({ key }),
        modifiers: 0,
      });
    }
    pressedModifiers.clear();
  };

  surface.addEventListener("pointerdown", onPointerDown);
  surface.addEventListener("pointerup", onPointerUp);
  surface.addEventListener("pointermove", onPointerMove);
  surface.addEventListener("contextmenu", onContextMenu);
  surface.addEventListener("wheel", onWheel, { passive: false });
  focusTarget.addEventListener("keydown", onKeyDown);
  focusTarget.addEventListener("keyup", onKeyUp);
  focusTarget.addEventListener("beforeinput", onBeforeInput);
  focusTarget.addEventListener("compositionstart", onCompositionStart);
  focusTarget.addEventListener("compositionend", onCompositionEnd);
  window.addEventListener("blur", onWindowBlur);

  return () => {
    if (moveFrame !== 0) cancelAnimationFrame(moveFrame);
    if (pendingMove) flushMove();
    surface.removeEventListener("pointerdown", onPointerDown);
    surface.removeEventListener("pointerup", onPointerUp);
    surface.removeEventListener("pointermove", onPointerMove);
    surface.removeEventListener("contextmenu", onContextMenu);
    surface.removeEventListener("wheel", onWheel);
    focusTarget.removeEventListener("keydown", onKeyDown);
    focusTarget.removeEventListener("keyup", onKeyUp);
    focusTarget.removeEventListener("beforeinput", onBeforeInput);
    focusTarget.removeEventListener("compositionstart", onCompositionStart);
    focusTarget.removeEventListener("compositionend", onCompositionEnd);
    window.removeEventListener("blur", onWindowBlur);
  };
}
