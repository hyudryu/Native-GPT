# @agentgpt/design-system

Design tokens and a Tailwind v4 (CSS-first) theme for AgentGPT.

## Usage

In the app's entry CSS:

```css
@import "@agentgpt/design-system/tokens.css";
@import "tailwindcss";
@import "@agentgpt/design-system/theme.css";
```

Set the theme on `<html>`:

```html
<html data-theme="light"> <!-- or "dark" -->
```

## What you get

- `tokens.css` — CSS custom properties: typography scale, 4px spacing scale,
  large radii, surface/elevation system, semantic colors, an Apple Messages
  blue accent, motion durations/easings, and a focus-ring token.
  Light theme on `:root`, dark theme under `[data-theme="dark"]`.
- `theme.css` — `@theme inline` mappings so Tailwind utilities
  (`bg-surface-1`, `text-fg-muted`, `border-border`, `rounded-xl`,
  `shadow-md`, `text-accent`, …) resolve to the token variables and flip
  with `data-theme` at runtime. No JS Tailwind config.

Visual direction: neutral graphite + warm white, minimal borders, subtle
shadows, large card radii. No neon, no gradients.
