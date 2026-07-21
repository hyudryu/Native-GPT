# Native GPT — Frontend Stack & Design System Guide

Everything another project needs to clone this look and feel. The design system is framework-portable: the tokens are plain CSS variables, the recipes are Tailwind v4 class strings.

---

## 1. Frontend stack

| Layer | Choice | Version |
|---|---|---|
| Framework | React + TypeScript (strict) | React 19, TS 7 |
| Bundler | Vite (`@vitejs/plugin-react`) | 8.x |
| Styling | Tailwind CSS v4 (`@tailwindcss/vite`) — **CSS-first, no JS config** | 4.x |
| Design tokens | Plain CSS custom properties (`tokens.css`), mapped into Tailwind via `@theme inline` (`theme.css`) | — |
| Primitives | Base UI (`@base-ui-components/react`) — unstyled accessible Dialog, etc. (Radix works too) | 1.x |
| Icons | `lucide-react` | 1.x |
| Client state | `zustand` | 5.x |
| Server state | `@tanstack/react-query` | 5.x |
| Routing | `react-router` (HashRouter for static serving) | 8.x |
| PWA | `vite-plugin-pwa` (app-shell cache only) | 1.x |
| Tests | `vitest` + jsdom | 4.x |

Why this combo: Tailwind v4's `@theme inline` reads the CSS variables **by reference**, so toggling `data-theme="dark"` on `<html>` re-themes every utility at runtime with zero CSS rebuild and zero JS theming code.

---

## 2. Design philosophy

- **Premium and quiet.** Apple-like: warm neutrals, one restrained accent, generous whitespace, almost no borders, soft shadows. No neon, no gradients, no "AI purple".
- **Surfaces do the work.** Hierarchy comes from 4 layered background surfaces, not outlines.
- **Large radii on containers** (cards 24–32px), **medium on controls** (12px), **full on pills/switches**.
- **Motion is subtle and short** (120–320ms, standard easing), and fully disabled under `prefers-reduced-motion`.
- **Touch-first:** every interactive target ≥ 44×44px (`min-h-11 min-w-11`), no hover-only interactions.
- **Mobile is the same codebase:** one responsive layout, breakpoint at 1024px (`lg`).

---

## 3. Tokens (the source of truth)

### 3.1 Color — light theme (`:root`)

| Token | Value | Use |
|---|---|---|
| `--surface-0` | `#faf9f7` | App background (warm white) |
| `--surface-1` | `#ffffff` | Cards, sidebar, inputs, assistant bubbles |
| `--surface-2` | `#f3f1ed` | Recessed areas, hover/active row fill |
| `--surface-3` | `#ffffff` | Overlays, sheets, dialogs |
| `--text-primary` (`fg`) | `#1c1c1e` | Primary text |
| `--text-secondary` (`fg-muted`) | `#5c5b57` | Secondary text, inactive rows |
| `--text-tertiary` (`fg-subtle`) | `#8f8d87` | Placeholders, section headers, disabled |
| `--text-inverse` | `#f7f5f1` | Text on dark fills |
| `--border` | `#e7e4de` | Hairline borders (use sparingly) |
| `--border-strong` | `#d8d4cc` | Switch tracks, radio rings |
| `--accent` | `#007aff` | Apple system blue — primary buttons, active states, user bubbles |
| `--accent-hover` | `#0065d1` | Accent hover |
| `--accent-contrast` | `#ffffff` | Text on accent |
| `--accent-subtle` | `#e5f2ff` | Accent tint background |
| `--success` / `-subtle` | `#337a4b` / `#e2efe6` | OK states, connected dot |
| `--warning` / `-subtle` | `#a16207` / `#f6ecd4` | Caution |
| `--danger` / `-subtle` | `#c2413b` / `#f7e3e1` | Errors, delete |
| `--info` / `-subtle` | `#4a6fa5` / `#e3eaf3` | Informational |

### 3.2 Color — dark theme (`[data-theme="dark"]`)

Graphite, same structure: `--surface-0 #131315`, `--surface-1 #1b1b1e`, `--surface-2 #232327`, `--surface-3 #26262b`; text `#f4f2ef / #a9a7a1 / #6f6d68`; borders `#2c2c31 / #3b3b41`; accent switches to Apple's dark-mode blue `#0a84ff` (hover `#409cff`, subtle `#082a4a`); semantic colors brighten (`--success #5fae7d`, `--warning #d9a83f`, `--danger #e0655f`, `--info #7f9cc4`) with dark tinted subtles.

### 3.3 Typography

- **Sans:** system stack — `ui-sans-serif, system-ui, -apple-system, "SF Pro Text", "Segoe UI", Roboto, …`
- **Mono:** `ui-monospace, SFMono-Regular, Menlo, Consolas, …` (used for model IDs, URLs)
- **Scale (rem):** xs `.75/1` · sm `.875/1.375` · base `1/1.5` · lg `1.125/1.625` · xl `1.25/1.75` · 2xl `1.5/2` · 3xl `1.875/2.25`
- **Weights:** 400 regular, 500 medium, 600 semibold. Headings use `font-semibold tracking-tight`.
- Section headers: `text-xs font-medium uppercase tracking-wide text-fg-subtle`.

### 3.4 Spacing, radii, elevation, motion

- **Spacing:** 4px base unit (`--spacing: 0.25rem`); scale 1,2,3,4,5,6,8,10,12,16,20,24.
- **Radii:** sm `6px` · md `10px` · lg `16px` · xl `24px` (cards, bubbles, composer) · 2xl `32px` (big sheets) · full (pills, switches, icon buttons are `rounded-xl`).
- **Shadows (light):**
  - `sm`: `0 1px 2px rgb(28 25 23 / .05)`
  - `md`: `0 1px 2px / .04, 0 4px 12px -2px / .08`
  - `lg`: `0 2px 4px / .04, 0 12px 32px -8px / .14` — dark theme swaps these for deeper black-alpha versions.
- **Focus ring:** `outline: 2px color-mix(in srgb, accent 45%, transparent)`, offset 2px.
- **Motion:** fast `120ms` · base `200ms` · slow `320ms`; easings `cubic-bezier(.25,.1,.25,1)` standard, `(.2,0,0,1)` emphasized, `(0,0,.2,1)` decelerate. All zeroed under reduced-motion.

---

## 4. How it wires into Tailwind v4

```css
/* app entry CSS */
@import "@your-scope/design-system/tokens.css";
@import "tailwindcss";
@import "@your-scope/design-system/theme.css";
```

`theme.css` maps tokens to utilities via `@theme inline { --color-surface-0: var(--surface-0); … }` so you write `bg-surface-1 text-fg-muted border-border rounded-xl shadow-md` everywhere. Runtime theme switch = one attribute:

```ts
document.documentElement.dataset.theme = "dark"; // or "light"
// persisted to localStorage; initial value follows prefers-color-scheme
```

---

## 5. Component recipes (actual class strings)

### 5.1 Buttons

```ts
// Primary (accent)
"inline-flex min-h-11 items-center gap-2 rounded-xl bg-accent px-4 text-sm
 font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"

// Small secondary (ghost-bordered)
"inline-flex min-h-9 items-center justify-center gap-1.5 rounded-xl border
 border-border bg-surface-1 px-3 text-xs text-fg-muted hover:bg-surface-2
 hover:text-fg disabled:opacity-50"

// Icon button
"inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl
 text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg"
```

### 5.2 List rows (sidebar conversations, settings links)

```ts
"flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-xl px-3 text-left
 text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg"
// active: + "bg-surface-2 text-fg"
```

### 5.3 Cards & panels

```ts
// Card
"rounded-xl border border-border bg-surface-1 p-3 shadow-sm"
// Inset panel inside a card (e.g. models list)
"rounded-xl border border-border bg-surface-0 p-3"
```

### 5.4 Chat bubbles

```ts
// User — solid accent, right-aligned
"ml-auto max-w-[88%] whitespace-pre-wrap rounded-2xl bg-accent px-4 py-3
 text-sm leading-relaxed text-accent-contrast"
// Assistant — quiet card, left-aligned
"mr-auto max-w-[88%] whitespace-pre-wrap rounded-2xl border border-border
 bg-surface-1 px-4 py-3 text-sm leading-relaxed text-fg"
```

### 5.5 Composer

```ts
// Floating bar: rounded-2xl card with a real shadow
"mx-auto flex w-full max-w-2xl items-end gap-2 rounded-2xl border
 border-border bg-surface-1 p-2 shadow-md"
// Textarea inside: transparent, no chrome
"max-h-40 min-h-11 flex-1 resize-none rounded-xl bg-transparent px-3 py-2.5
 text-base text-fg placeholder:text-fg-subtle"
```

### 5.6 Status pill & dot

```ts
// Pill (hidden in tight spaces — dot-only variant with tooltip)
"inline-flex max-w-full min-w-0 items-center gap-2 overflow-hidden rounded-full
 border border-border bg-surface-1 px-3 py-1 text-xs text-fg-muted shadow-sm"
// Dot: size-2 rounded-full bg-success (ping layer when transitional)
```

### 5.7 Toggle switch (iOS-style)

```ts
// Track: h-7 w-12 rounded-full; accent when on, surface-2 + border-strong when off
// Knob: absolute left-0.5 top-0.5 size-5 rounded-full bg-white shadow-sm,
//       translate-x-5 when on, transition-transform duration-150
```

### 5.8 Inputs

```ts
"min-h-11 w-full rounded-xl border border-border bg-surface-0 px-3 text-sm
 text-fg placeholder:text-fg-subtle"
// mono variant (URLs, model IDs): + "font-mono text-xs"
```

### 5.9 Dialogs (Base UI): centered card on desktop, bottom sheet on mobile

```ts
backdrop: "fixed inset-0 z-40 bg-black/40 transition-opacity duration-200
           data-[ending-style]:opacity-0 data-[starting-style]:opacity-0"
popup:    "fixed z-50 bg-surface-3 shadow-lg outline-none transition-opacity duration-200
           max-sm:inset-x-0 max-sm:bottom-0 max-sm:max-h-[85dvh] max-sm:rounded-t-2xl
           sm:left-1/2 sm:top-1/2 sm:max-w-md sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-2xl"
```

### 5.10 Section headers & wordmark

```ts
// Section header (Pinned / Projects / Chats)
"px-3 py-2 text-xs font-medium uppercase tracking-wide text-fg-subtle"
// Wordmark: bold name + muted suffix
// <span class="text-base font-semibold tracking-tight">Native
//   <span class="font-normal text-fg-subtle">GPT</span></span>
```

### 5.11 Error & alert text

```ts
"rounded-xl bg-danger-subtle p-3 text-sm text-danger"   // block
"text-xs text-danger"                                    // inline, role="alert"
```

---

## 6. Layout patterns

### 6.1 App shell (breakpoint `lg` = 1024px)

```
┌────────────┬──────────────────────────────┐
│ Sidebar    │  Header (title + model)      │  ≥1024px: sidebar 288px (full),
│ (Pinned/   ├──────────────────────────────┤   80px (compact icons), or 0 (hidden
│ Projects/  │  Content, max-w-2xl column   │   + floating reopen button).
│ Chats/     │  centered in the space       │  <1024px: top bar + hamburger
│ Settings + │ ┌──────────────────────────┐ │   sheet + bottom tab nav instead.
│ theme row) │ │ Composer (sticky bottom) │ │
└────────────┴─┴──────────────────────────┴─┘
```

- Root: `flex h-dvh bg-surface-0 text-fg` (always `dvh`, never `vh` — mobile keyboards).
- Sidebar column: `bg-surface-1` with hairline `border-r border-border`; scrollable middle `min-h-0 flex-1 overflow-y-auto px-2`; footer `border-t border-border p-2`.
- Settings + theme toggle share one footer row (toggle right-aligned).
- Sidebar width animates between modes: `transition-[width] duration-200 ease-standard`, inner `overflow-hidden`.

### 6.2 Mobile rules

- Bottom nav: icon + 10px label per tab, `min-h-11`, active = `text-accent`.
- Slide-over sheet: `fixed inset-y-0 left-0 w-72 max-w-[85vw] bg-surface-3 shadow-lg`, slides with `data-[starting-style]:-translate-x-full`.
- Safe areas everywhere the OS can intrude: `padding-top: env(safe-area-inset-top)`, `padding-bottom: max(env(safe-area-inset-bottom), 1rem)`.
- `viewport-fit=cover`, dialogs become bottom sheets (`max-sm:` variant above).

### 6.3 Page content

- Settings: single centered column `mx-auto max-w-2xl px-4`, sections as cards with `text-lg font-semibold` headings.
- Chat: messages column `mx-auto w-full max-w-2xl gap-4`, empty state centered icon-in-rounded-square (`size-14 rounded-2xl bg-surface-1 shadow-sm` + `size-7` lucide icon).

---

## 7. Accessibility & quality bar

- Every icon-only button has `aria-label`; decorative icons get `aria-hidden`.
- Toggle = `role="switch" aria-checked`; default-model picker = `role="radio"` in a `role="radiogroup"`; status = `role="status"`.
- Errors use `role="alert"`, loading regions `aria-busy`, streams `aria-live="polite"`.
- Keyboard: Enter sends / Shift+Enter newline; focus rings via tokens; `prefers-reduced-motion` zeroes all durations.
- Disabled states: `disabled:opacity-40/50/60` + `cursor-not-allowed` for dead buttons; unreleased features get `(coming soon)` tooltips and `text-fg-subtle/50`.

---

## 8. To clone this into another app

1. `pnpm add tailwindcss @tailwindcss/vite @base-ui-components/react lucide-react zustand @tanstack/react-query react-router` (+ `vite-plugin-pwa` if PWA).
2. Copy `tokens.css` + `theme.css` verbatim; import them before/after `tailwindcss` as in §4.
3. Copy the recipe strings from §5 into shared constants (this project keeps them in small `const` strings next to components, plus `dialogStyles.ts`).
4. Add the theme module (§4 snippet: get/set/toggle with localStorage + `prefers-color-scheme`) and a pre-paint inline script in `index.html` that sets `data-theme` to avoid FOUC.
5. Follow the layout skeleton in §6.1 and the mobile rules in §6.2.
6. Icons: lucide at `size-5` (nav/buttons), `size-4` (compact), `size-7` (empty states).
