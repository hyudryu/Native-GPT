import { NavLink } from "react-router";
import { appsRegistry } from "./appsRegistry";

const row =
  "flex min-h-11 min-w-0 items-center gap-2 rounded-xl px-3 text-left text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg";

export default function AppsMenu({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <section aria-labelledby="apps-heading">
      <h2
        id="apps-heading"
        className="px-3 py-2 text-xs font-medium uppercase tracking-wide text-fg-subtle"
      >
        Apps
      </h2>
      <ul className="space-y-0.5">
        {appsRegistry.map((app) => {
          const Icon = app.icon;
          return (
            <li key={app.id}>
              {app.external ? (
                <a href={app.href} target="_blank" rel="noreferrer" onClick={onNavigate} className={row}>
                  <Icon className="size-4 shrink-0" aria-hidden />
                  <span className="truncate">{app.name}</span>
                </a>
              ) : (
                <NavLink
                  to={app.href}
                  onClick={onNavigate}
                  className={({ isActive }) => `${row} ${isActive ? "bg-surface-2 text-fg" : ""}`}
                >
                  <Icon className="size-4 shrink-0" aria-hidden />
                  <span className="truncate">{app.name}</span>
                </NavLink>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
