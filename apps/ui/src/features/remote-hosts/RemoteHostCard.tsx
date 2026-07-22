import {
  Check,
  LoaderCircle,
  Pencil,
  PlugZap,
  Trash2,
} from "lucide-react";
import {
  ApiError,
  useTestRemoteHost,
  type RemoteHost,
} from "../../lib/remoteHosts";

const actionBtn =
  "inline-flex min-h-9 items-center gap-1.5 rounded-xl border border-border bg-surface-1 px-3 text-xs text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg disabled:opacity-50";

function StatusDot({ status }: { status: RemoteHost["status"] }) {
  const color =
    status === "reachable"
      ? "bg-success"
      : status === "unreachable"
        ? "bg-danger"
        : "bg-fg-subtle";
  const label = status ?? "unknown";
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-fg-muted">
      <span className={`size-2 rounded-full ${color}`} />
      {label}
    </span>
  );
}

function WorkloadBadges({ host }: { host: RemoteHost }) {
  const workloads = host.workloads;
  if (!workloads || Object.keys(workloads).length === 0) {
    return <p className="mt-2 text-xs text-fg-subtle">No workloads reported</p>;
  }
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {Object.entries(workloads).map(([id, wl]) => (
        <span
          key={id}
          className="inline-flex items-center gap-1 rounded-lg bg-surface-2 px-2 py-0.5 text-xs text-fg-muted"
        >
          <span
            className={`size-1.5 rounded-full ${wl.healthy ? "bg-success" : "bg-fg-subtle"}`}
          />
          {id}
          <span className="text-fg-subtle">· {wl.state}</span>
        </span>
      ))}
    </div>
  );
}

export default function RemoteHostCard({
  host,
  onEdit,
  onDelete,
}: {
  host: RemoteHost;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const test = useTestRemoteHost();

  return (
    <article
      aria-labelledby={`host-${host.id}-name`}
      className="rounded-2xl border border-border bg-surface-1 p-4 shadow-sm"
    >
      <div className="flex flex-col items-start justify-between gap-2 sm:flex-row sm:gap-3">
        <div className="min-w-0">
          <h3
            id={`host-${host.id}-name`}
            className="truncate text-sm font-semibold text-fg"
          >
            {host.name}
          </h3>
          <p className="mt-0.5 truncate font-mono text-xs text-fg-muted">
            {host.base_url}
          </p>
        </div>
        <StatusDot status={host.status} />
      </div>

      <p className="mt-2 text-xs text-fg-subtle">
        Token:{" "}
        <span className="font-mono text-fg-muted">
          {host.has_token ? "configured" : "—"}
        </span>
        {!host.tls_verify && (
          <span className="ml-2 text-warning">TLS verify off</span>
        )}
      </p>

      <WorkloadBadges host={host} />

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onEdit}
          aria-label={`Edit host ${host.name}`}
          className={actionBtn}
        >
          <Pencil className="size-3.5" aria-hidden />
          Edit
        </button>
        <button
          type="button"
          onClick={() => test.mutate(host.id)}
          aria-label={`Test host ${host.name}`}
          disabled={test.isPending}
          className={actionBtn}
        >
          {test.isPending ? (
            <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <PlugZap className="size-3.5" aria-hidden />
          )}
          Test connection
        </button>
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Delete host ${host.name}`}
          className="inline-flex min-h-9 items-center gap-1.5 rounded-xl border border-border bg-surface-1 px-3 text-xs text-danger transition-colors duration-150 hover:bg-danger-subtle"
        >
          <Trash2 className="size-3.5" aria-hidden />
          Delete
        </button>
      </div>

      {/* Inline test result */}
      {test.isSuccess &&
        (test.data.ok ? (
          <p className="mt-2 inline-flex items-center gap-1 text-xs text-success">
            <Check className="size-3.5" aria-hidden />
            Reachable
            {test.data.version && ` · v${test.data.version}`}
            {test.data.latency_ms !== undefined &&
              ` · ${test.data.latency_ms} ms`}
          </p>
        ) : (
          <p role="alert" className="mt-2 text-xs text-danger">
            {test.data.error
              ? `${test.data.error.code}: ${test.data.error.message}`
              : "Connection failed"}
          </p>
        ))}
      {test.isError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          {test.error instanceof ApiError
            ? `${test.error.code}: ${test.error.message}`
            : "Test request failed"}
        </p>
      )}
    </article>
  );
}
