import { describe, expect, it } from "vitest";
import {
  MODIFIER_ALT,
  MODIFIER_CTRL,
  MODIFIER_META,
  MODIFIER_SHIFT,
  modifiersMask,
  mouseButtonName,
  windowsVirtualKeyCode,
} from "./inputBridge";

describe("modifiersMask", () => {
  it("maps modifier flags to the CDP bitmask (Alt=1 Ctrl=2 Meta=4 Shift=8)", () => {
    expect(
      modifiersMask({ altKey: false, ctrlKey: false, metaKey: false, shiftKey: false }),
    ).toBe(0);
    expect(
      modifiersMask({ altKey: true, ctrlKey: false, metaKey: false, shiftKey: false }),
    ).toBe(MODIFIER_ALT);
    expect(
      modifiersMask({ altKey: false, ctrlKey: true, metaKey: false, shiftKey: false }),
    ).toBe(MODIFIER_CTRL);
    expect(
      modifiersMask({ altKey: false, ctrlKey: false, metaKey: true, shiftKey: false }),
    ).toBe(MODIFIER_META);
    expect(
      modifiersMask({ altKey: false, ctrlKey: false, metaKey: false, shiftKey: true }),
    ).toBe(MODIFIER_SHIFT);
  });

  it("combines multiple modifiers", () => {
    expect(
      modifiersMask({ altKey: true, ctrlKey: true, metaKey: true, shiftKey: true }),
    ).toBe(15);
    expect(
      modifiersMask({ altKey: false, ctrlKey: true, metaKey: false, shiftKey: true }),
    ).toBe(10);
  });
});

describe("mouseButtonName", () => {
  it("maps DOM button numbers to CDP button names", () => {
    expect(mouseButtonName(0)).toBe("left");
    expect(mouseButtonName(1)).toBe("middle");
    expect(mouseButtonName(2)).toBe("right");
    expect(mouseButtonName(3)).toBe("none");
  });
});

describe("windowsVirtualKeyCode", () => {
  it("maps common non-printable keys", () => {
    expect(windowsVirtualKeyCode({ key: "Enter" })).toBe(13);
    expect(windowsVirtualKeyCode({ key: "Escape" })).toBe(27);
    expect(windowsVirtualKeyCode({ key: "Backspace" })).toBe(8);
    expect(windowsVirtualKeyCode({ key: "Tab" })).toBe(9);
    expect(windowsVirtualKeyCode({ key: "Delete" })).toBe(46);
    expect(windowsVirtualKeyCode({ key: "ArrowLeft" })).toBe(37);
    expect(windowsVirtualKeyCode({ key: "ArrowUp" })).toBe(38);
    expect(windowsVirtualKeyCode({ key: "ArrowRight" })).toBe(39);
    expect(windowsVirtualKeyCode({ key: "ArrowDown" })).toBe(40);
    expect(windowsVirtualKeyCode({ key: "Home" })).toBe(36);
    expect(windowsVirtualKeyCode({ key: "End" })).toBe(35);
    expect(windowsVirtualKeyCode({ key: "PageUp" })).toBe(33);
    expect(windowsVirtualKeyCode({ key: "PageDown" })).toBe(34);
    expect(windowsVirtualKeyCode({ key: " " })).toBe(32);
    expect(windowsVirtualKeyCode({ key: "F5" })).toBe(116);
    expect(windowsVirtualKeyCode({ key: "F12" })).toBe(123);
  });

  it("derives codes for printable characters", () => {
    expect(windowsVirtualKeyCode({ key: "a" })).toBe(65);
    expect(windowsVirtualKeyCode({ key: "A" })).toBe(65);
    expect(windowsVirtualKeyCode({ key: "7" })).toBe(55);
  });

  it("returns 0 for unknown keys", () => {
    expect(windowsVirtualKeyCode({ key: "Unidentified" })).toBe(0);
  });
});
