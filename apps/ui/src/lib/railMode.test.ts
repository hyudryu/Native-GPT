import { describe, it, expect, beforeEach } from "vitest";
import { useRailModeStore, RAIL_MODE_KEY } from "./railMode";

describe("railMode store", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useRailModeStore.setState({ mode: "full", lastMode: "full" });
  });

  it("cycles full → compact → hidden → full", () => {
    const s = useRailModeStore.getState();
    s.cycle();
    expect(useRailModeStore.getState().mode).toBe("compact");
    useRailModeStore.getState().cycle();
    expect(useRailModeStore.getState().mode).toBe("hidden");
    useRailModeStore.getState().cycle();
    expect(useRailModeStore.getState().mode).toBe("full");
  });

  it("persists the mode to localStorage", () => {
    useRailModeStore.getState().setMode("compact");
    expect(window.localStorage.getItem(RAIL_MODE_KEY)).toBe("compact");
  });

  it("tracks the last non-hidden mode for restore()", () => {
    const s = useRailModeStore.getState();
    s.setMode("compact");
    s.setMode("hidden");
    expect(useRailModeStore.getState().lastMode).toBe("compact");
    useRailModeStore.getState().restore();
    expect(useRailModeStore.getState().mode).toBe("compact");
  });

  it("going hidden directly from full restores full", () => {
    useRailModeStore.getState().setMode("hidden");
    useRailModeStore.getState().restore();
    expect(useRailModeStore.getState().mode).toBe("full");
  });
});
