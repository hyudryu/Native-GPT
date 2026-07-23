import { useState } from "react";
import { Dialog } from "@base-ui-components/react/dialog";
import { LoaderCircle, Plus, Server, X } from "lucide-react";
import {
  ApiError,
  useDeleteRemoteHost,
  useRemoteHosts,
  type RemoteHost,
} from "../../lib/remoteHosts";
import { dialogBackdropCls, dialogPopupCls } from "../../components/dialogStyles";
import RemoteHostCard from "./RemoteHostCard";
import RemoteHostFormDialog from "./RemoteHostFormDialog";

function DeleteConfirmDialog({
  host,
  onClose,
}: {
  host: RemoteHost | null;
  onClose: () => void;
}) {
  const del = useDeleteRemoteHost();

  return (
    <Dialog.Root
      open={host !== null}
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
                Delete remote host
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
              <span className="font-medium text-fg">{host?.name}</span> (
              <span className="font-mono text-xs">{host?.base_url}</span>)? This
              also removes its generated assets.
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
                  host && del.mutate(host.id, { onSuccess: onClose })
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

export default function RemoteHostsSection() {
  const hosts = useRemoteHosts();
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<RemoteHost | null>(null);
  const [deleting, setDeleting] = useState<RemoteHost | null>(null);

  return (
    <section
      aria-labelledby="settings-remote-hosts"
      className="mt-6 rounded-2xl border border-border bg-surface-1 p-5 shadow-sm"
    >
      <div className="flex flex-col items-stretch justify-between gap-3 sm:flex-row sm:items-center">
        <div className="flex items-center gap-2">
          <Server className="size-5 text-fg-subtle" aria-hidden />
          <h2 id="settings-remote-hosts" className="text-lg font-medium">
            Remote Hosts
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
          Add host
        </button>
      </div>

      <div className="mt-4 space-y-3">
        {hosts.isPending && (
          <p className="flex items-center gap-2 text-sm text-fg-subtle">
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
            Loading remote hosts…
          </p>
        )}
        {hosts.isError && (
          <p
            role="alert"
            className="rounded-xl bg-danger-subtle px-3 py-2 text-sm text-danger"
          >
            {hosts.error instanceof ApiError
              ? `${hosts.error.code}: ${hosts.error.message}`
              : "Failed to load remote hosts"}
          </p>
        )}
        {hosts.isSuccess && hosts.data.length === 0 && (
          <p className="text-sm text-fg-muted">
            No remote hosts yet. Add one to connect a GPU backend for image
            generation and voice synthesis.
          </p>
        )}
        {hosts.data?.map((host) => (
          <RemoteHostCard
            key={host.id}
            host={host}
            onEdit={() => {
              setEditing(host);
              setFormOpen(true);
            }}
            onDelete={() => setDeleting(host)}
          />
        ))}
      </div>

      <RemoteHostFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        host={editing}
      />
      <DeleteConfirmDialog host={deleting} onClose={() => setDeleting(null)} />
    </section>
  );
}
