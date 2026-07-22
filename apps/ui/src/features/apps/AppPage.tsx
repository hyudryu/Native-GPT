import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

export default function AppPage({ title, description, icon: Icon, actions, children }: { title: string; description: string; icon: LucideIcon; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="h-full min-h-0 overflow-y-auto overscroll-contain">
      <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6">
        <header className="flex flex-wrap items-center gap-4">
          <span className="flex size-12 items-center justify-center rounded-2xl bg-accent text-white"><Icon className="size-6" aria-hidden /></span>
          <div className="min-w-0 flex-1"><h1 className="text-2xl font-semibold tracking-tight">{title}</h1><p className="mt-1 text-sm text-fg-muted">{description}</p></div>
          {actions}
        </header>
        <div className="mt-7">{children}</div>
      </div>
    </div>
  );
}

export const panel = "rounded-2xl border border-border bg-surface-1 p-5 shadow-sm";
export const primaryButton = "inline-flex min-h-11 items-center justify-center gap-2 rounded-xl bg-accent px-4 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50";
export const secondaryButton = "inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-border bg-surface-1 px-4 text-sm font-medium text-fg-muted hover:bg-surface-2 hover:text-fg disabled:opacity-50";
export const field = "min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg outline-none focus:border-accent";
