import { describe, expect, it, vi } from "vitest";
import { handleTitleBarMouseDown, type WindowDragController } from "./WindowTitleBar";

function controller() {
  return {
    startDragging: vi.fn().mockResolvedValue(undefined),
    toggleMaximize: vi.fn().mockResolvedValue(undefined),
  } satisfies WindowDragController;
}

describe("handleTitleBarMouseDown", () => {
  it("starts dragging on a primary-button press", async () => {
    const appWindow = controller();

    await expect(handleTitleBarMouseDown(appWindow, 0, 1)).resolves.toBe(true);
    expect(appWindow.startDragging).toHaveBeenCalledOnce();
    expect(appWindow.toggleMaximize).not.toHaveBeenCalled();
  });

  it("toggles maximize on a primary-button double click", async () => {
    const appWindow = controller();

    await expect(handleTitleBarMouseDown(appWindow, 0, 2)).resolves.toBe(true);
    expect(appWindow.toggleMaximize).toHaveBeenCalledOnce();
    expect(appWindow.startDragging).not.toHaveBeenCalled();
  });

  it("ignores non-primary mouse buttons", async () => {
    const appWindow = controller();

    await expect(handleTitleBarMouseDown(appWindow, 2, 1)).resolves.toBe(false);
    expect(appWindow.startDragging).not.toHaveBeenCalled();
    expect(appWindow.toggleMaximize).not.toHaveBeenCalled();
  });
});
