import { useState } from "react";
import { Dialog } from "@base-ui-components/react/dialog";
import { LoaderCircle, Plus, Server, X } from "lucide-react";
import {
  ApiError,
  useDeleteEndpoint,
  useEndpoints,
  type Endpoint,
} from "../../lib/endpoints";
import { dialogBackdropCls, dialogPopupCls } from "../../components/dialogStyles";
import EndpointCard from "./EndpointCard";
import EndpointFormDialog from "./EndpointFormDialog";

function DeleteConfirmDialog({
  endpoint,
  onClose,
}: {
  endpoint: Endpoint | null;
  onClose: () => void;
}) {
  const del = useDeleteEndpoint();

  return (
    <Dialog.Root
      open={endpoint !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className={dialogBackdropCls} />
        <Dialog.Popup className={dialogPopupCls}>
          <div className="p-5">
            <div className="flex items-center justify-between">
              <Dialog.Title className="text-lg font-semibold tracking-tight">
                Delete provider
              </Dialog.Title>
              <Dialog.Close
                aria-label="Close"
                className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-fg-muted hover:bg-surface-2 hover:text-fg"
              >
                <X className="size-5" aria-hidden />
              </Dialog.Close>
            </div>
            <Dialog.Description className="mt-2 text-sm text-fg-muted">
              Delete{" "}
              <span className="font-medium text-fg">{endpoint?.name}</span>{" "}
              (<span className="font-mono text-xs">{endpoint?.base_url}</span>)?
              This also removes its model configuration.
            </Dialog.Description>
            {del.isError && (
              <p role="alert" className="mt-3 text-sm text-danger">
                {del.error instanceof ApiError
                  ? `${del.error.code}: ${del.error.message}`
                  : "Delete failed"}
              </p>
            )}
            <div className="mt-6 flex justify-end gap-2">
              <Dialog.Close className="min-h-11 rounded-xl px-4 text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg">
                Cancel
              </Dialog.Close>
              <button
                type="button"
                disabled={del.isPending}
                onClick={() =>
                  endpoint &&
                  del.mutate(endpoint.id, { onSuccess: onClose })
                }
                className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-danger px-4 text-sm font-medium text-white transition-opacity duration-150 hover:opacity-90 disabled:opacity-50"
              >
                {del.isPending && (
                  <LoaderCircle className="size-4 animate-spin" aria-hidden />
                )}
                Delete
              </button>
            </div>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export default function EndpointsSection() {
  const endpoints = useEndpoints();
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Endpoint | null>(null);
  const [deleting, setDeleting] = useState<Endpoint | null>(null);

  return (
    <section
      aria-labelledby="settings-providers"
      className="mt-6 rounded-2xl border border-border bg-surface-1 p-5 shadow-sm"
    >
      <div className="flex flex-col items-stretch justify-between gap-3 sm:flex-row sm:items-center">
        <div className="flex items-center gap-2">
          <Server className="size-5 text-fg-subtle" aria-hidden />
          <h2 id="settings-providers" className="text-lg font-medium">
            Providers
          </h2>
        </div>
        <button
          type="button"
          onClick={() => {
            setEditing(null);
            setFormOpen(true);
          }}
          className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl bg-accent px-3 text-sm font-medium text-accent-contrast transition-colors duration-150 hover:bg-accent-hover"
        >
          <Plus className="size-4" aria-hidden />
          Add provider
        </button>
      </div>

      <div className="mt-4 space-y-3">
        {endpoints.isPending && (
          <p className="flex items-center gap-2 text-sm text-fg-subtle">
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
            Loading providers…
          </p>
        )}
        {endpoints.isError && (
          <p role="alert" className="rounded-xl bg-danger-subtle px-3 py-2 text-sm text-danger">
            {endpoints.error instanceof ApiError
              ? `${endpoints.error.code}: ${endpoints.error.message}`
              : "Failed to load providers"}
          </p>
        )}
        {endpoints.isSuccess && endpoints.data.length === 0 && (
          <p className="text-sm text-fg-muted">
            No providers yet. Add one to connect a model service.
          </p>
        )}
        {endpoints.data?.map((ep) => (
          <EndpointCard
            key={ep.id}
            endpoint={ep}
            onEdit={() => {
              setEditing(ep);
              setFormOpen(true);
            }}
            onDelete={() => setDeleting(ep)}
          />
        ))}
      </div>

      <EndpointFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        endpoint={editing}
      />
      <DeleteConfirmDialog
        endpoint={deleting}
        onClose={() => setDeleting(null)}
      />
    </section>
  );
}
