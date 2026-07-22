import { ChevronDown, Grid2X2 } from "lucide-react";
import { NavLink } from "react-router";
import { appsRegistry } from "./appsRegistry";

export default function AppsMenu({ onNavigate }: { onNavigate?: () => void }) {
  const select = (element: HTMLElement) => {
    const details = element.closest("details") as HTMLDetailsElement | null;
    if (details) details.open = false;
    onNavigate?.();
  };
  return (
    <details className="group relative px-2">
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl px-3 text-sm font-medium text-fg-muted hover:bg-surface-2 hover:text-fg [&::-webkit-details-marker]:hidden">
        <Grid2X2 className="size-5" aria-hidden />
        <span>Apps</span>
        <ChevronDown className="ml-auto size-4 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <div className="absolute left-3 right-3 top-full z-30 mt-1 overflow-hidden rounded-2xl border border-border bg-surface-3 p-1.5 shadow-lg">
        {appsRegistry.map((app) => {
          const Icon = app.icon;
          const content = (
            <>
              <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent text-white"><Icon className="size-4" aria-hidden /></span>
              <span className="min-w-0"><span className="block text-sm font-medium text-fg">{app.name}</span><span className="block truncate text-xs text-fg-subtle">{app.description}</span></span>
            </>
          );
          return app.external ? (
            <a key={app.id} href={app.href} target="_blank" rel="noreferrer" onClick={(event) => select(event.currentTarget)} className="flex min-h-12 items-center gap-2 rounded-xl px-2 hover:bg-surface-2">{content}</a>
          ) : (
            <NavLink key={app.id} to={app.href} onClick={(event) => select(event.currentTarget)} className={({ isActive }) => `flex min-h-12 items-center gap-2 rounded-xl px-2 hover:bg-surface-2 ${isActive ? "bg-surface-2" : ""}`}>{content}</NavLink>
          );
        })}
      </div>
    </details>
  );
}
