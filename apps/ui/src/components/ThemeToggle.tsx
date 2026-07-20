import { useState } from "react";
import { Moon, Sun } from "lucide-react";
import { getTheme, toggleTheme, type Theme } from "../lib/theme";

export default function ThemeToggle({ className = "" }: { className?: string }) {
  const [theme, setThemeState] = useState<Theme>(() => getTheme());
  const Icon = theme === "dark" ? Sun : Moon;

  return (
    <button
      type="button"
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      onClick={() => setThemeState(toggleTheme())}
      className={`inline-flex min-h-11 min-w-11 items-center justify-center rounded-full text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg ${className}`}
    >
      <Icon className="size-5" aria-hidden />
    </button>
  );
}
