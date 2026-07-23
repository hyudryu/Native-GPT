import { Switch } from "@base-ui-components/react/switch";

/**
 * Universal iOS-style toggle for boolean settings (enabled/trusted/TLS/…).
 * Replaces checkbox inputs for on/off states across the app so every
 * boolean switch looks and behaves the same.
 *
 * The `toggle-switch` class keeps the pill shape under the global
 * `*:focus-visible` radius override (see index.css).
 */
export default function Toggle({
  checked,
  onCheckedChange,
  disabled = false,
  label,
  className = "",
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  /** Accessible name for the switch (visible label text is fine). */
  label: string;
  className?: string;
}) {
  return (
    <Switch.Root
      aria-label={label}
      checked={checked}
      disabled={disabled}
      onCheckedChange={(next) => onCheckedChange(next)}
      className={`toggle-switch inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border border-border bg-surface-2 p-0.5 transition-colors duration-200 data-[checked]:border-transparent data-[checked]:bg-accent disabled:cursor-not-allowed disabled:opacity-50 ${className}`}
    >
      <Switch.Thumb className="block size-5 rounded-full bg-white shadow-sm transition-transform duration-200 data-[checked]:translate-x-5" />
    </Switch.Root>
  );
}
