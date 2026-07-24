import { useEffect, useState } from "react";
import { Dialog } from "@base-ui-components/react/dialog";
import { useQueryClient } from "@tanstack/react-query";
import { Check, LoaderCircle, PlugZap, X } from "lucide-react";
import {
  ApiError,
  listModels,
  useCreateEndpoint,
  useTestEndpoint,
  useUpdateEndpoint,
  type Endpoint,
} from "../../lib/endpoints";
import {
  DEFAULT_TIMEOUT_SECONDS,
  formatThinkingParams,
  hasErrors,
  toEndpointPayload,
  validateEndpointForm,
  type EndpointFormErrors,
  type EndpointFormValues,
} from "../../lib/validateEndpoint";
import { dialogBackdropCls, dialogPopupCls } from "../../components/dialogStyles";
import { persistProviderForTest } from "../../lib/providerTestFlow";

const inputCls =
  "min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg placeholder:text-fg-subtle";
const labelCls = "mb-1 block text-sm font-medium text-fg-muted";
const errorCls = "mt-1 text-xs text-danger";

const emptyValues: EndpointFormValues = {
  name: "",
  base_url: "",
  api_key: "",
  clear_key: false,
  timeout_seconds: String(DEFAULT_TIMEOUT_SECONDS),
  thinking_off_params: "",
  thinking_high_params: "",
};

function valuesFromEndpoint(endpoint: Endpoint): EndpointFormValues {
  return {
    name: endpoint.name,
    base_url: endpoint.base_url,
    api_key: "",
    clear_key: false,
    timeout_seconds: String(endpoint.timeout_seconds),
    thinking_off_params: formatThinkingParams(endpoint.thinking_off_params_json),
    thinking_high_params: formatThinkingParams(endpoint.thinking_high_params_json),
  };
}

export default function EndpointFormDialog({
  open,
  onOpenChange,
  endpoint,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** null = add mode; otherwise edit this provider. */
  endpoint: Endpoint | null;
}) {
  const editing = endpoint !== null;
  const [values, setValues] = useState<EndpointFormValues>(emptyValues);
  const [errors, setErrors] = useState<EndpointFormErrors>({});
  const [persistedEndpoint, setPersistedEndpoint] = useState<Endpoint | null>(endpoint);
  const [modelsFound, setModelsFound] = useState<number | null>(null);
  const [flowError, setFlowError] = useState<unknown>(null);
  const [isTesting, setIsTesting] = useState(false);

  const queryClient = useQueryClient();
  const create = useCreateEndpoint();
  const update = useUpdateEndpoint();
  const test = useTestEndpoint();

  useEffect(() => {
    if (open) {
      setValues(endpoint ? valuesFromEndpoint(endpoint) : emptyValues);
      setErrors({});
      setPersistedEndpoint(endpoint);
      setModelsFound(null);
      setFlowError(null);
      setIsTesting(false);
      create.reset();
      update.reset();
      test.reset();
    }
    // Mutation objects are intentionally omitted; only a dialog/target change resets state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, endpoint]);

  const saved = persistedEndpoint !== null;
  const mutation = saved ? update : create;
  const apiError = flowError instanceof ApiError ? flowError : null;

  const set = <K extends keyof EndpointFormValues>(
    key: K,
    value: EndpointFormValues[K],
  ) => {
    setValues((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: undefined }));
    setFlowError(null);
    setModelsFound(null);
  };

  const validate = () => {
    const nextErrors = validateEndpointForm(values);
    setErrors(nextErrors);
    return !hasErrors(nextErrors);
  };

  const endpointInput = () => ({
    ...toEndpointPayload(values),
    ...(values.api_key ? { api_key: values.api_key } : {}),
  });

  const persistProvider = async () => {
    const input = endpointInput();
    const next = await persistProviderForTest(persistedEndpoint, input, {
      create: (createInput) => create.mutateAsync(createInput),
      update: (id, updateInput) => update.mutateAsync({ id, input: updateInput }),
    });
    setPersistedEndpoint(next);
    return next;
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!validate()) return;
    setFlowError(null);
    void persistProvider()
      .then(() => onOpenChange(false))
      .catch(setFlowError);
  };

  const testConnection = async () => {
    if (!validate()) return;
    setFlowError(null);
    setModelsFound(null);
    setIsTesting(true);
    try {
      // The test route needs an ID. Persist once, then update/reuse that provider
      // for subsequent tests so repeated clicks cannot create duplicates.
      const next = await persistProvider();
      const result = await test.mutateAsync(next.id);
      if (!result.ok) return;

      // Current hosts return the discovered models with the test. Fall back to
      // an explicit refresh for older hosts.
      const modelData = result.models
        ? {
            models: result.models,
            fetched_at: result.fetched_at ?? new Date().toISOString(),
          }
        : await listModels(next.id, true);
      queryClient.setQueryData(["endpoints", next.id, "models"], modelData);
      void queryClient.invalidateQueries({ queryKey: ["models", "enabled"] });
      setModelsFound(modelData.models.length);
    } catch (error) {
      setFlowError(error);
    } finally {
      setIsTesting(false);
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className={dialogBackdropCls} />
        <Dialog.Popup className={dialogPopupCls}>
          <form onSubmit={submit} className="p-5" noValidate>
            <div className="flex items-center justify-between">
              <Dialog.Title className="text-lg font-semibold tracking-tight">
                {editing ? "Edit provider" : "Add provider"}
              </Dialog.Title>
              <Dialog.Close
                aria-label="Close"
                className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-fg-muted hover:bg-surface-2 hover:text-fg"
              >
                <X className="size-5" aria-hidden />
              </Dialog.Close>
            </div>

            <div className="mt-4 space-y-4">
              <div>
                <label htmlFor="ep-name" className={labelCls}>Name</label>
                <input
                  id="ep-name"
                  className={inputCls}
                  value={values.name}
                  onChange={(event) => set("name", event.target.value)}
                  placeholder="My provider"
                  autoComplete="off"
                />
                {errors.name && <p className={errorCls}>{errors.name}</p>}
              </div>

              <div>
                <label htmlFor="ep-url" className={labelCls}>URL</label>
                <input
                  id="ep-url"
                  className={`${inputCls} font-mono`}
                  value={values.base_url}
                  onChange={(event) => set("base_url", event.target.value)}
                  placeholder="http://127.0.0.1:8080/v1"
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                />
                {errors.base_url && <p className={errorCls}>{errors.base_url}</p>}
              </div>

              <div>
                <label htmlFor="ep-key" className={labelCls}>API key</label>
                <input
                  id="ep-key"
                  type="password"
                  className={inputCls}
                  value={values.api_key}
                  onChange={(event) => set("api_key", event.target.value)}
                  placeholder={
                    editing && endpoint.has_api_key
                      ? "Stored — enter a new key to replace"
                      : "Optional"
                  }
                  autoComplete="off"
                />
              </div>

              <div>
                <label htmlFor="ep-thinking-off" className={labelCls}>
                  Thinking-off params override
                </label>
                <textarea
                  id="ep-thinking-off"
                  rows={3}
                  className={`${inputCls} resize-y py-2 font-mono text-xs`}
                  value={values.thinking_off_params}
                  onChange={(event) => set("thinking_off_params", event.target.value)}
                  placeholder='{"reasoning_effort": "none"}'
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="mt-1 text-xs text-fg-subtle">
                  Optional JSON object merged into the request when thinking mode is Off.
                </p>
                {errors.thinking_off_params && (
                  <p className={errorCls}>{errors.thinking_off_params}</p>
                )}
              </div>

              <div>
                <label htmlFor="ep-thinking-high" className={labelCls}>
                  Thinking-high params override
                </label>
                <textarea
                  id="ep-thinking-high"
                  rows={3}
                  className={`${inputCls} resize-y py-2 font-mono text-xs`}
                  value={values.thinking_high_params}
                  onChange={(event) => set("thinking_high_params", event.target.value)}
                  placeholder='{"reasoning_effort": "high"}'
                  autoComplete="off"
                  spellCheck={false}
                />
                <p className="mt-1 text-xs text-fg-subtle">
                  Optional JSON object merged into the request when thinking mode is On.
                </p>
                {errors.thinking_high_params && (
                  <p className={errorCls}>{errors.thinking_high_params}</p>
                )}
              </div>
            </div>

            {flowError !== null && (
              <p role="alert" className="mt-4 rounded-xl bg-danger-subtle px-3 py-2 text-sm text-danger">
                {apiError ? `${apiError.code}: ${apiError.message}` : "Request failed"}
              </p>
            )}

            <div className="mt-4">
              <div className="flex flex-wrap items-center gap-2" aria-live="polite">
                <button
                  type="button"
                  onClick={() => void testConnection()}
                  disabled={isTesting || mutation.isPending}
                  className="inline-flex min-h-11 items-center gap-2 rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg disabled:opacity-50"
                >
                  {isTesting ? (
                    <LoaderCircle className="size-4 animate-spin" aria-hidden />
                  ) : (
                    <PlugZap className="size-4" aria-hidden />
                  )}
                  Test connection
                </button>
                {!flowError && test.isSuccess && (
                  test.data.ok ? (
                    <span className="inline-flex items-center gap-1 text-sm text-success">
                      <Check className="size-4" aria-hidden />
                      {test.data.latency_ms !== undefined ? `${test.data.latency_ms} ms` : "OK"}
                      {modelsFound !== null && ` · ${modelsFound} model${modelsFound === 1 ? "" : "s"}`}
                    </span>
                  ) : (
                    <span className="text-sm text-danger">
                      {test.data.error
                        ? `${test.data.error.code}: ${test.data.error.message}`
                        : "Connection failed"}
                    </span>
                  )
                )}
              </div>
              {!editing && saved && (
                <p className="mt-2 text-xs text-fg-subtle">
                  This provider is saved. You can test again or close this dialog.
                </p>
              )}
            </div>

            <div className="mt-6 flex flex-wrap justify-end gap-2">
              <Dialog.Close className="min-h-11 rounded-xl px-4 text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg">
                {saved ? "Close" : "Cancel"}
              </Dialog.Close>
              <button
                type="submit"
                disabled={mutation.isPending || isTesting}
                className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast transition-colors duration-150 hover:bg-accent-hover disabled:opacity-50"
              >
                {mutation.isPending && <LoaderCircle className="size-4 animate-spin" aria-hidden />}
                {saved ? "Save and close" : "Save"}
              </button>
            </div>
          </form>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
