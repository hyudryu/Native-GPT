/**
 * Thinking-mode selection (design spec §6.1-2): three modes — off / high / max —
 * plus a Max-only depth preset. Both are per-message; the picker selection is
 * persisted in localStorage as the default for the next send.
 *
 * Migrates the retired `agentgpt.thinkingLevel` stub key:
 * low → off, medium → high, high → high.
 */

export type ThinkingMode = "off" | "high" | "max";
export type MaxDepth = "quick" | "standard" | "deep";

export const THINKING_MODES: readonly ThinkingMode[] = ["off", "high", "max"];
export const MAX_DEPTHS: readonly MaxDepth[] = ["quick", "standard", "deep"];

export const THINKING_MODE_STORAGE_KEY = "agentgpt.thinkingMode";
export const MAX_DEPTH_STORAGE_KEY = "agentgpt.maxDepth";

const LEGACY_LEVEL_STORAGE_KEY = "agentgpt.thinkingLevel";

/** Retired ThinkingLevel stub values → new mode (spec §6.1). */
const LEGACY_LEVEL_MAP: Record<string, ThinkingMode> = {
  low: "off",
  medium: "high",
  high: "high",
};

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function defaultStorage(): StorageLike | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function isThinkingMode(value: string): value is ThinkingMode {
  return value === "off" || value === "high" || value === "max";
}

/**
 * Read the persisted mode, migrating a legacy `agentgpt.thinkingLevel` value
 * (and removing the old key) on first access. Defaults to "high".
 */
export function loadThinkingMode(
  storage: StorageLike | null = defaultStorage(),
): ThinkingMode {
  if (!storage) return "high";
  try {
    const stored = storage.getItem(THINKING_MODE_STORAGE_KEY);
    if (stored !== null && isThinkingMode(stored)) return stored;
    const legacy = storage.getItem(LEGACY_LEVEL_STORAGE_KEY);
    if (legacy !== null) {
      storage.removeItem(LEGACY_LEVEL_STORAGE_KEY);
      const migrated = LEGACY_LEVEL_MAP[legacy];
      if (migrated) {
        storage.setItem(THINKING_MODE_STORAGE_KEY, migrated);
        return migrated;
      }
    }
  } catch {
    // Storage unavailable (private mode, etc.) — fall through to the default.
  }
  return "high";
}

export function saveThinkingMode(
  mode: ThinkingMode,
  storage: StorageLike | null = defaultStorage(),
): void {
  try {
    storage?.setItem(THINKING_MODE_STORAGE_KEY, mode);
  } catch {
    // Storage unavailable — keep the in-memory selection only.
  }
}

/** Read the persisted Max depth. Defaults to "standard" (spec §3.4). */
export function loadMaxDepth(
  storage: StorageLike | null = defaultStorage(),
): MaxDepth {
  if (!storage) return "standard";
  try {
    const stored = storage.getItem(MAX_DEPTH_STORAGE_KEY);
    if (stored === "quick" || stored === "standard" || stored === "deep") return stored;
  } catch {
    // Storage unavailable — fall through to the default.
  }
  return "standard";
}

export function saveMaxDepth(
  depth: MaxDepth,
  storage: StorageLike | null = defaultStorage(),
): void {
  try {
    storage?.setItem(MAX_DEPTH_STORAGE_KEY, depth);
  } catch {
    // Storage unavailable — keep the in-memory selection only.
  }
}
