import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  ChevronRight,
  LoaderCircle,
  Pencil,
  PlugZap,
  Trash2,
} from "lucide-react";
import {
  ApiError,
  listModels,
  useTestEndpoint,
  type Endpoint,
} from "../../lib/endpoints";
import { describeEndpointStatus, type StatusTone } from "../../lib/relTime";
import ModelsPanel from "./ModelsPanel";

const DOT: Record<StatusTone, string> = {
  ok: "bg-success",
  danger: "bg-danger",
  muted: "bg-fg-subtle",
};

const actionBtn =
  "inline-flex min-h-9 items-center gap-1.5 rounded-xl border border-border bg-surface-1 px-3 text-xs text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg disabled:opacity-50";

export default function EndpointCard({
  endpoint,
  onEdit,
  onDelete,
}: {
  endpoint: Endpoint;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [modelsOpen, setModelsOpen] = useState(true);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const test = useTestEndpoint();
  const modelsPanelId = `provider-${endpoint.id}-models`;
  const status = describeEndpointStatus(
    endpoint.last_test_status,
    endpoint.last_tested_at,
  );

  return (
    <article
      aria-labelledby={`provider-${endpoint.id}-name`}
      className="rounded-2xl border border-border bg-surface-1 p-4 shadow-sm"
    >
      <div className="flex flex-col items-start justify-between gap-2 sm:flex-row sm:gap-3">
        <div className="min-w-0">
          <h3
            id={`provider-${endpoint.id}-name`}
            className="truncate text-sm font-semibold text-fg"
          >
            {endpoint.name}
          </h3>
          <p className="mt-0.5 truncate font-mono text-xs text-fg-muted">
            {endpoint.base_url}
          </p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-fg-muted">
          <span className={`size-2 rounded-full ${DOT[status.tone]}`} />
          {status.label}
        </span>
      </div>

      <p className="mt-2 text-xs text-fg-subtle">
        Default model:{" "}
        <span className="font-mono text-fg-muted">
          {endpoint.default_model_id ?? "—"}
        </span>
      </p>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center">
        <button
          type="button"
          onClick={onEdit}
          aria-label={`Edit provider ${endpoint.name}`}
          className={actionBtn}
        >
          <Pencil className="size-3.5" aria-hidden />
          Edit
        </button>
        <button
          type="button"
          onClick={() => {
            setRefreshError(null);
            test.mutate(endpoint.id, {
              onSuccess: (result) => {
                if (!result.ok) return;
                void listModels(endpoint.id, true).then((modelData) => {
                  queryClient.setQueryData(
                    ["endpoints", endpoint.id, "models"],
                    modelData,
                  );
                  void queryClient.invalidateQueries({ queryKey: ["models", "enabled"] });
                }).catch((error: unknown) => {
                  setRefreshError(
                    error instanceof ApiError ? error.message : "Models could not be refreshed",
                  );
                });
              },
            });
          }}
          aria-label={`Test provider ${endpoint.name}`}
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
          onClick={() => setModelsOpen((v) => !v)}
          aria-expanded={modelsOpen}
          aria-controls={modelsPanelId}
          className={actionBtn}
        >
          {modelsOpen ? (
            <ChevronDown className="size-3.5" aria-hidden />
          ) : (
            <ChevronRight className="size-3.5" aria-hidden />
          )}
          Models
        </button>
        <button
          type="button"
          onClick={onDelete}
          aria-label={`Delete provider ${endpoint.name}`}
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
            Connection OK
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
      {refreshError && (
        <p role="alert" className="mt-2 text-xs text-danger">
          Connection succeeded, but models could not be fetched: {refreshError}
        </p>
      )}

      {modelsOpen && (
        <div id={modelsPanelId}>
          <ModelsPanel endpoint={endpoint} />
        </div>
      )}
    </article>
  );
}
