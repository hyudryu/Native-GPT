import { describe, expect, it } from "vitest";
import {
  loadMaxDepth,
  loadThinkingMode,
  MAX_DEPTH_STORAGE_KEY,
  saveMaxDepth,
  saveThinkingMode,
  THINKING_MODE_STORAGE_KEY,
} from "./thinkingMode";

const LEGACY_LEVEL_STORAGE_KEY = "agentgpt.thinkingLevel";

/** Minimal in-memory Storage stand-in for deterministic tests. */
function fakeStorage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    removeItem: (key: string) => void map.delete(key),
  };
}

describe("loadThinkingMode", () => {
  it("defaults to high with no stored value", () => {
    expect(loadThinkingMode(fakeStorage())).toBe("high");
  });

  it("returns a stored mode", () => {
    for (const mode of ["off", "high", "max"] as const) {
      expect(
        loadThinkingMode(fakeStorage({ [THINKING_MODE_STORAGE_KEY]: mode })),
      ).toBe(mode);
    }
  });

  it("ignores unrecognized stored values", () => {
    expect(
      loadThinkingMode(fakeStorage({ [THINKING_MODE_STORAGE_KEY]: "ultra" })),
    ).toBe("high");
  });

  it("migrates legacy thinkingLevel values (low→off, medium→high, high→high)", () => {
    expect(loadThinkingMode(fakeStorage({ [LEGACY_LEVEL_STORAGE_KEY]: "low" }))).toBe("off");
    expect(loadThinkingMode(fakeStorage({ [LEGACY_LEVEL_STORAGE_KEY]: "medium" }))).toBe("high");
    expect(loadThinkingMode(fakeStorage({ [LEGACY_LEVEL_STORAGE_KEY]: "high" }))).toBe("high");
  });

  it("writes the migrated value to the new key and removes the old one", () => {
    const storage = fakeStorage({ [LEGACY_LEVEL_STORAGE_KEY]: "low" });
    loadThinkingMode(storage);
    expect(storage.map.get(THINKING_MODE_STORAGE_KEY)).toBe("off");
    expect(storage.map.has(LEGACY_LEVEL_STORAGE_KEY)).toBe(false);
  });

  it("prefers the new key over the legacy key", () => {
    const storage = fakeStorage({
      [THINKING_MODE_STORAGE_KEY]: "max",
      [LEGACY_LEVEL_STORAGE_KEY]: "low",
    });
    expect(loadThinkingMode(storage)).toBe("max");
  });

  it("falls back to high when storage is unavailable", () => {
    expect(loadThinkingMode(null)).toBe("high");
  });
});

describe("saveThinkingMode", () => {
  it("persists the selection", () => {
    const storage = fakeStorage();
    saveThinkingMode("max", storage);
    expect(loadThinkingMode(storage)).toBe("max");
  });
});

describe("max depth", () => {
  it("defaults to standard", () => {
    expect(loadMaxDepth(fakeStorage())).toBe("standard");
    expect(loadMaxDepth(null)).toBe("standard");
  });

  it("round-trips a stored depth and ignores junk", () => {
    const storage = fakeStorage();
    saveMaxDepth("deep", storage);
    expect(loadMaxDepth(storage)).toBe("deep");
    expect(loadMaxDepth(fakeStorage({ [MAX_DEPTH_STORAGE_KEY]: "extreme" }))).toBe("standard");
  });
});
