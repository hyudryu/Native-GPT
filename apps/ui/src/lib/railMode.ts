import { create } from "zustand";

/**
 * Desktop sidebar display mode, persisted to localStorage so the choice
 * survives restarts. Shared via zustand so AppShell and the Settings
 * "Appearance" section stay in sync without a reload.
 */

export type RailMode = "full" | "compact" | "hidden";
export type VisibleRailMode = Exclude<RailMode, "hidden">;

export const RAIL_MODE_KEY = "agentgpt.railMode";

function isRailMode(value: unknown): value is RailMode {
  return value === "full" || value === "compact" || value === "hidden";
}

function readInitialMode(): RailMode {
  try {
    const stored = window.localStorage.getItem(RAIL_MODE_KEY);
    if (isRailMode(stored)) return stored;
  } catch {
    /* storage unavailable */
  }
  return "full";
}

function persist(mode: RailMode): void {
  try {
    window.localStorage.setItem(RAIL_MODE_KEY, mode);
  } catch {
    /* storage unavailable — session-only */
  }
}

interface RailModeState {
  mode: RailMode;
  /** Last non-hidden mode — what the floating reopen button restores. */
  lastMode: VisibleRailMode;
  setMode: (mode: RailMode) => void;
  /** full → compact → hidden (→ full). */
  cycle: () => void;
  /** Restore the last non-hidden mode (floating button). */
  restore: () => void;
}

const initial = readInitialMode();

export const useRailModeStore = create<RailModeState>((set, get) => ({
  mode: initial,
  lastMode: initial === "hidden" ? "full" : initial,
  setMode: (mode) => {
    persist(mode);
    set((s) => ({
      mode,
      lastMode: mode === "hidden" ? s.lastMode : mode,
    }));
  },
  cycle: () => {
    const order: Record<RailMode, RailMode> = {
      full: "compact",
      compact: "hidden",
      hidden: "full",
    };
    get().setMode(order[get().mode]);
  },
  restore: () => {
    get().setMode(get().lastMode);
  },
}));
