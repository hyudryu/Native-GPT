import { useState } from "react";
import { Check, LoaderCircle, Plus, RefreshCw } from "lucide-react";
import {
  ApiError,
  useAddModel,
  useDiscoverModels,
  useModels,
  useUpdateEndpoint,
  useUpdateModel,
  type Endpoint,
  type ModelInfo,
} from "../../lib/endpoints";

const smallBtn =
  "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-xl border border-border bg-surface-1 px-3 text-xs text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg disabled:opacity-50";

function errorText(error: unknown): string {
  return error instanceof ApiError
    ? `${error.code}: ${error.message}`
    : "Request failed";
}

function ModelRow({ endpoint, model }: { endpoint: Endpoint; model: ModelInfo }) {
  const updateEndpoint = useUpdateEndpoint();
  const updateModel = useUpdateModel(endpoint.id);
  const isDefault = endpoint.default_model_id === model.id;
  const enabled = !model.hidden;

  return (
    <li className="rounded-xl px-2 py-1 hover:bg-surface-1">
      <div className="flex min-h-11 flex-wrap items-center gap-2">
        <button
          type="button"
          role="radio"
          aria-checked={isDefault}
          aria-label={
            isDefault
              ? `${model.id} is the default model`
              : `Set ${model.id} as default`
          }
          title={isDefault ? "Default model" : "Set as default"}
          disabled={model.hidden || updateEndpoint.isPending}
          onClick={() =>
            updateEndpoint.mutate({
              id: endpoint.id,
              input: { default_model_id: model.id },
            })
          }
          className="inline-flex min-h-11 min-w-11 items-center justify-center text-fg-subtle hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
        >
          {isDefault ? (
            <Check className="size-4 text-accent" aria-hidden />
          ) : (
            <span className="size-4 rounded-full border border-border-strong" />
          )}
        </button>
        <span
          className={`min-w-32 flex-1 break-all font-mono text-xs ${
            enabled ? "text-fg" : "text-fg-subtle"
          }`}
        >
          {model.id}
        </span>
        {model.source === "manual" && (
          <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-fg-subtle">
            manual
          </span>
        )}
        <span className="text-xs text-fg-muted" aria-hidden>
          {enabled ? "Enabled" : "Disabled"}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={`${model.id} enabled`}
          disabled={updateModel.isPending}
          onClick={() =>
            updateModel.mutate({ modelId: model.id, hidden: enabled })
          }
          className={`relative h-7 w-12 shrink-0 rounded-full border transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50 ${
            enabled
              ? "border-accent bg-accent"
              : "border-border-strong bg-surface-2"
          }`}
        >
          <span
            className={`absolute left-0.5 top-0.5 size-5 rounded-full bg-white shadow-sm transition-transform duration-150 ${
              enabled ? "translate-x-5" : "translate-x-0"
            }`}
          />
        </button>
      </div>
      {(updateModel.isError || updateEndpoint.isError) && (
        <p role="alert" className="pb-1 pl-11 text-xs text-danger">
          {errorText(updateModel.error ?? updateEndpoint.error)}
        </p>
      )}
    </li>
  );
}

export default function ModelsPanel({ endpoint }: { endpoint: Endpoint }) {
  const models = useModels(endpoint.id, true);
  const discover = useDiscoverModels(endpoint.id);
  const addModel = useAddModel(endpoint.id);
  const [manualId, setManualId] = useState("");

  const list = models.data?.models ?? discover.data?.models ?? [];

  return (
    <div className="mt-3 rounded-xl border border-border bg-surface-0 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-medium text-fg">Models</h4>
        <button
          type="button"
          onClick={() => discover.mutate()}
          disabled={discover.isPending}
          className={smallBtn}
        >
          {discover.isPending ? (
            <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <RefreshCw className="size-3.5" aria-hidden />
          )}
          Refresh models
        </button>
      </div>

      <details className="mt-2 rounded-xl border border-border bg-surface-1 px-3 py-2">
        <summary className="min-h-9 cursor-pointer content-center text-xs font-medium text-fg-muted">
          Advanced: add a model manually
        </summary>
        <form
          className="mt-2 flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center"
          onSubmit={(event) => {
            event.preventDefault();
            const id = manualId.trim();
            if (!id) return;
            addModel.mutate(id, { onSuccess: () => setManualId("") });
          }}
        >
          <input
            value={manualId}
            onChange={(event) => setManualId(event.target.value)}
            placeholder="Model ID"
            aria-label="Manual model ID"
            className="min-h-9 min-w-0 flex-1 rounded-xl border border-border bg-surface-0 px-3 font-mono text-xs text-fg placeholder:text-fg-subtle"
          />
          <button
            type="submit"
            disabled={addModel.isPending || manualId.trim().length === 0}
            className={smallBtn}
          >
            {addModel.isPending ? (
              <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
            ) : (
              <Plus className="size-3.5" aria-hidden />
            )}
            Add
          </button>
        </form>
      </details>

      {(models.isError || discover.isError || addModel.isError) && (
        <p role="alert" className="mt-2 text-xs text-danger">
          {models.isError && errorText(models.error)}
          {discover.isError && errorText(discover.error)}
          {addModel.isError && errorText(addModel.error)}
        </p>
      )}

      {models.isPending ? (
        <p className="mt-3 flex items-center gap-2 text-xs text-fg-subtle">
          <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
          Loading models…
        </p>
      ) : list.length === 0 ? (
        <p className="mt-3 text-xs text-fg-subtle">
          No models yet — refresh the provider or add one manually.
        </p>
      ) : (
        <ul role="radiogroup" aria-label="Default model" className="mt-2">
          {list.map((model) => (
            <ModelRow key={model.id} endpoint={endpoint} model={model} />
          ))}
        </ul>
      )}
    </div>
  );
}
