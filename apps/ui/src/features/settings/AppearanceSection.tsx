import { Monitor } from "lucide-react";
import ThemeToggle from "../../components/ThemeToggle";
import { useRailModeStore, type RailMode } from "../../lib/railMode";

const RAIL_MODES: Array<{ value: RailMode; label: string; hint: string }> = [
  { value: "full", label: "Full", hint: "Labels + lists" },
  { value: "compact", label: "Compact", hint: "Icons only" },
  { value: "hidden", label: "Hidden", hint: "Floating button" },
];

export default function AppearanceSection() {
  const mode = useRailModeStore((s) => s.mode);
  const setMode = useRailModeStore((s) => s.setMode);

  return (
    <section
      aria-labelledby="settings-appearance"
      className="mt-6 rounded-2xl border border-border bg-surface-1 p-5 shadow-sm"
    >
      <div className="flex items-center gap-2">
        <Monitor className="size-5 text-fg-subtle" aria-hidden />
        <h2 id="settings-appearance" className="text-lg font-medium">
          Appearance
        </h2>
      </div>

      <div className="mt-4 space-y-4">
        <div>
          <span className="mb-1 block text-sm font-medium text-fg-muted">
            Sidebar (desktop)
          </span>
          <div
            role="radiogroup"
            aria-label="Sidebar display mode"
            className="flex flex-col gap-2 sm:flex-row"
          >
            {RAIL_MODES.map((opt) => {
              const checked = mode === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  role="radio"
                  aria-checked={checked}
                  onClick={() => setMode(opt.value)}
                  className={`flex min-h-11 flex-1 items-center gap-3 rounded-xl border px-3 text-left transition-colors duration-150 ${
                    checked
                      ? "border-accent bg-accent-subtle"
                      : "border-border bg-surface-1 hover:bg-surface-2"
                  }`}
                >
                  <span
                    className={`size-4 shrink-0 rounded-full border ${
                      checked
                        ? "border-accent bg-accent"
                        : "border-border-strong"
                    }`}
                  />
                  <span>
                    <span className="block text-sm text-fg">{opt.label}</span>
                    <span className="block text-xs text-fg-subtle">
                      {opt.hint}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
          <p className="mt-2 text-xs text-fg-subtle">
            Applies on desktop (≥1024px). Mobile keeps the bottom navigation.
          </p>
        </div>

        <div className="flex items-center justify-between gap-4">
          <div>
            <span className="block text-sm font-medium text-fg-muted">
              Theme
            </span>
            <span className="block text-xs text-fg-subtle">
              Switch between light and dark
            </span>
          </div>
          <ThemeToggle />
        </div>
      </div>
    </section>
  );
}
