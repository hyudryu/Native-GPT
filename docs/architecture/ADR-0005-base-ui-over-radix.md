# ADR-0005: Base UI instead of Radix primitives

**Status:** Accepted (2026-07-20)

## Context

The design system needs accessible, unstyled primitives. Radix UI development slowed after the WorkOS acquisition; shadcn/ui switched its default to Base UI (MUI's headless layer, v1.0 Dec 2025) in mid-2026.

## Decision

Use `@base-ui-components/react` for new primitive wrappers (Dialog, Popover, Tabs, Switch, Select, Tooltip). Radix 1.1 remains an acceptable fallback if Base UI lacks a needed primitive. All primitives are wrapped behind app components in `packages/design-system` so the underlying library can be swapped.

## Consequences

- (+) Actively maintained; same headless + Tailwind styling model.
- (−) Younger ecosystem, fewer examples than Radix.
