import { useHealth } from "../lib/api";
import { useWsStore, type ConnectionState } from "../lib/ws";

type Tone = "ok" | "warn" | "muted";

const DOT: Record<Tone, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  muted: "bg-fg-subtle",
};

function describe(state: ConnectionState, apiDegraded: boolean, apiDown: boolean): { label: string; tone: Tone; pulse: boolean } {
  switch (state) {
    case "open":
      if (apiDown) return { label: "API unreachable", tone: "warn", pulse: false };
      if (apiDegraded) return { label: "Degraded", tone: "warn", pulse: false };
      return { label: "Connected", tone: "ok", pulse: false };
    case "connecting":
      return { label: "Connecting…", tone: "muted", pulse: true };
    case "reconnecting":
      return { label: "Reconnecting…", tone: "warn", pulse: true };
    default:
      return { label: "Offline", tone: "muted", pulse: false };
  }
}

/** Live status: WebSocket connection state + /api/health. */
export default function StatusPill({ compact = false }: { compact?: boolean }) {
  const wsState = useWsStore((s) => s.state);
  const health = useHealth();
  const { label, tone, pulse } = describe(
    wsState,
    health.data?.status === "degraded",
    health.isError,
  );

  const dot = (
    <span className="relative flex size-2">
      {pulse && (
        <span
          className={`absolute inline-flex size-full animate-ping rounded-full opacity-60 ${DOT[tone]}`}
        />
      )}
      <span className={`relative inline-flex size-2 rounded-full ${DOT[tone]}`} />
    </span>
  );

  if (compact) {
    return (
      <span role="status" title={label} aria-label={label} className="p-1">
        {dot}
      </span>
    );
  }

  return (
    <span
      role="status"
      className="inline-flex max-w-full min-w-0 items-center gap-2 overflow-hidden rounded-full border border-border bg-surface-1 px-3 py-1 text-xs text-fg-muted shadow-sm"
    >
      {dot}
      <span className="truncate">{label}</span>
    </span>
  );
}
