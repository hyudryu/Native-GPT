import { useEffect, useState } from "react";
import { Dialog } from "@base-ui-components/react/dialog";
import { LoaderCircle, X } from "lucide-react";
import {
  ApiError,
  useCreateRemoteHost,
  useUpdateRemoteHost,
  type RemoteHost,
} from "../../lib/remoteHosts";
import { dialogBackdropCls, dialogPopupCls } from "../../components/dialogStyles";

const inputCls =
  "min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg placeholder:text-fg-subtle";
const labelCls = "mb-1 block text-sm font-medium text-fg-muted";
const errorCls = "mt-1 text-xs text-danger";

interface FormValues {
  name: string;
  base_url: string;
  token: string;
  tls_verify: boolean;
}

interface FormErrors {
  name?: string;
  base_url?: string;
}

const emptyValues: FormValues = {
  name: "",
  base_url: "",
  token: "",
  tls_verify: true,
};

function valuesFromHost(host: RemoteHost): FormValues {
  return {
    name: host.name,
    base_url: host.base_url,
    token: "",
    tls_verify: host.tls_verify,
  };
}

function validate(values: FormValues): FormErrors {
  const errors: FormErrors = {};
  if (!values.name.trim()) errors.name = "Name is required";
  if (!values.base_url.trim()) errors.base_url = "URL is required";
  else if (!/^https?:\/\//.test(values.base_url.trim()))
    errors.base_url = "URL must start with http:// or https://";
  return errors;
}

export default function RemoteHostFormDialog({
  open,
  onOpenChange,
  host,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** null = add mode; otherwise edit this host. */
  host: RemoteHost | null;
}) {
  const editing = host !== null;
  const [values, setValues] = useState<FormValues>(emptyValues);
  const [errors, setErrors] = useState<FormErrors>({});
  const [flowError, setFlowError] = useState<unknown>(null);

  const create = useCreateRemoteHost();
  const update = useUpdateRemoteHost();

  useEffect(() => {
    if (open) {
      setValues(host ? valuesFromHost(host) : emptyValues);
      setErrors({});
      setFlowError(null);
      create.reset();
      update.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, host]);

  const mutation = editing ? update : create;
  const apiError = flowError instanceof ApiError ? flowError : null;

  const set = <K extends keyof FormValues>(key: K, value: FormValues[K]) => {
    setValues((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: undefined }));
    setFlowError(null);
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const nextErrors = validate(values);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setFlowError(null);
    const payload = {
      name: values.name.trim(),
      base_url: values.base_url.trim(),
      tls_verify: values.tls_verify,
      ...(values.token ? { token: values.token } : {}),
    };

    const promise = editing
      ? update.mutateAsync({ id: host!.id, input: payload })
      : create.mutateAsync(payload);

    void promise.then(() => onOpenChange(false)).catch(setFlowError);
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className={dialogBackdropCls} />
        <Dialog.Popup className={dialogPopupCls}>
          <form onSubmit={submit} className="p-5" noValidate>
            <div className="flex items-center justify-between">
              <Dialog.Title className="text-lg font-semibold tracking-tight">
                {editing ? "Edit remote host" : "Add remote host"}
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
                <label htmlFor="rh-name" className={labelCls}>
                  Name
                </label>
                <input
                  id="rh-name"
                  className={inputCls}
                  value={values.name}
                  onChange={(e) => set("name", e.target.value)}
                  placeholder="DGX Spark"
                  autoComplete="off"
                />
                {errors.name && <p className={errorCls}>{errors.name}</p>}
              </div>

              <div>
                <label htmlFor="rh-url" className={labelCls}>
                  Bridge URL
                </label>
                <input
                  id="rh-url"
                  className={`${inputCls} font-mono`}
                  value={values.base_url}
                  onChange={(e) => set("base_url", e.target.value)}
                  placeholder="https://dgx.local:8443"
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                />
                {errors.base_url && <p className={errorCls}>{errors.base_url}</p>}
              </div>

              <div>
                <label htmlFor="rh-token" className={labelCls}>
                  Bridge token
                </label>
                <input
                  id="rh-token"
                  type="password"
                  className={inputCls}
                  value={values.token}
                  onChange={(e) => set("token", e.target.value)}
                  placeholder={
                    editing && host.has_token
                      ? "Stored — enter a new token to replace"
                      : "Bearer token from the bridge"
                  }
                  autoComplete="off"
                />
              </div>

              <label className="flex items-center gap-2 text-sm text-fg-muted">
                <input
                  type="checkbox"
                  checked={values.tls_verify}
                  onChange={(e) => set("tls_verify", e.target.checked)}
                  className="size-4 rounded border-border"
                />
                Verify TLS certificates
              </label>
            </div>

            <p className="mt-3 text-xs text-fg-subtle">
              The bridge runs on a Linux GPU host (e.g. DGX Spark). Add its URL
              and the bearer token it was started with. Workloads (ComfyUI,
              OpenVoice) start on demand and stop after idle.
            </p>

            {flowError !== null && (
              <p
                role="alert"
                className="mt-4 rounded-xl bg-danger-subtle px-3 py-2 text-sm text-danger"
              >
                {apiError ? `${apiError.code}: ${apiError.message}` : "Request failed"}
              </p>
            )}

            <div className="mt-6 flex flex-wrap justify-end gap-2">
              <Dialog.Close className="min-h-11 rounded-xl px-4 text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg">
                Cancel
              </Dialog.Close>
              <button
                type="submit"
                disabled={mutation.isPending}
                className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast transition-colors duration-150 hover:bg-accent-hover disabled:opacity-50"
              >
                {mutation.isPending && (
                  <LoaderCircle className="size-4 animate-spin" aria-hidden />
                )}
                {editing ? "Save" : "Add host"}
              </button>
            </div>
          </form>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
