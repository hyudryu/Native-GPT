import { Link } from "react-router";
import { Compass } from "lucide-react";

export default function NotFoundPage() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
      <div className="flex size-14 items-center justify-center rounded-2xl bg-surface-1 shadow-sm">
        <Compass className="size-7 text-fg-subtle" aria-hidden />
      </div>
      <h1 className="text-xl font-semibold tracking-tight">Page not found</h1>
      <p className="max-w-sm text-sm text-fg-muted">
        That route doesn't exist in this app.
      </p>
      <Link
        to="/"
        className="mt-2 inline-flex min-h-11 items-center rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast transition-colors duration-150 hover:bg-accent-hover"
      >
        Back to chat
      </Link>
    </div>
  );
}
