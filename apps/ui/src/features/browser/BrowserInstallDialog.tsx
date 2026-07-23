import { Dialog } from "@base-ui-components/react/dialog";
import { Download, LoaderCircle } from "lucide-react";
import {
  dialogBackdropCls,
  dialogPopupCls,
} from "../../components/dialogStyles";
import {
  useBrowserComponent,
  useInstallBrowserComponent,
} from "./browserApi";
import { useBrowserStore } from "./browserStore";
import type { InstallStatus } from "./types";

const STATUS_TEXT: Record<InstallStatus, string> = {
  not_installed: "Not installed",
  downloading: "Downloading…",
  verifying: "Verifying checksum…",
  extracting: "Extracting…",
  ready: "Installed",
  error: "Installation failed",
};

/**
 * Optional-component install dialog (spec §12.2): explains what is installed,
 * shows live install progress, and supports retry on failure. The server does
 * not expose install cancellation yet, so Cancel is disabled.
 */
export default function BrowserInstallDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const component = useBrowserComponent();
  const install = useInstallBrowserComponent();

  // Live install progress arrives over the browser stream into the store.
  const installStatus = useBrowserStore((s) => s.installStatus);
  const installProgress = useBrowserStore((s) => s.installProgress);
  const installError = useBrowserStore((s) => s.installError);

  const info = component.data;
  const status: InstallStatus = info?.status ?? installStatus;
  const progress = info?.progress ?? installProgress;
  const error = info?.error ?? installError;
  const installing =
    status === "downloading" || status === "verifying" || status === "extracting";

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className={dialogBackdropCls} />
        <Dialog.Popup className={dialogPopupCls}>
          <div className="p-5">
            <div className="flex items-center gap-2">
              <Download className="size-5 text-fg-subtle" aria-hidden />
              <Dialog.Title className="text-lg font-semibold">
                Install Native GPT Browser
              </Dialog.Title>
            </div>
            <Dialog.Description className="mt-2 text-sm text-fg-muted">
              Installs a dedicated Chromium runtime with Alibaba Page Agent
              support. Browser profile data is stored separately and is not
              removed during updates.
            </Dialog.Description>

            <dl className="mt-4 space-y-2 text-sm">
              <div className="flex items-center justify-between gap-4">
                <dt className="text-fg-muted">Download size</dt>
                <dd className="text-fg">
                  {/* The component manifest does not publish sizes yet. */}
                  Not yet published
                </dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-fg-muted">Disk after installation</dt>
                <dd className="text-fg">Not yet published</dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-fg-muted">Version</dt>
                <dd className="text-fg">
                  {info?.availableVersion ?? "…"}
                  {info?.installedVersion &&
                    ` (installed: ${info.installedVersion})`}
                </dd>
              </div>
            </dl>

            {(installing || status === "error" || status === "ready") && (
              <div className="mt-4">
                <div className="flex items-center gap-2 text-sm text-fg-muted">
                  {installing && (
                    <LoaderCircle className="size-4 animate-spin" aria-hidden />
                  )}
                  <span role="status">{STATUS_TEXT[status]}</span>
                  {status === "downloading" && progress != null && (
                    <span className="text-xs text-fg-subtle">
                      {Math.round(progress * 100)}%
                    </span>
                  )}
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-2">
                  <div
                    className="h-full rounded-full bg-accent transition-[width]"
                    style={{
                      width: `${
                        status === "ready"
                          ? 100
                          : Math.round((progress ?? 0) * 100)
                      }%`,
                    }}
                  />
                </div>
                {status === "error" && error && (
                  <p role="alert" className="mt-2 text-sm text-danger">
                    {error}
                  </p>
                )}
              </div>
            )}
            {/* The stream/store is the source of truth for the install status.
                Only surface the mutation error until the store catches up, to
                avoid two overlapping error paragraphs (install.isError + status
                === "error"). */}
            {install.isError && !(status === "error" && error) && (
              <p role="alert" className="mt-2 text-sm text-danger">
                {install.error.message}
              </p>
            )}

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                disabled
                title="Install cancellation is not supported by the server yet"
                className="min-h-11 cursor-not-allowed rounded-xl px-4 text-sm text-fg-muted opacity-50"
              >
                Cancel
              </button>
              {status === "ready" ? (
                <Dialog.Close className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover">
                  Done
                </Dialog.Close>
              ) : (
                <button
                  type="button"
                  onClick={() => install.mutate()}
                  disabled={installing || install.isPending}
                  className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"
                >
                  {status === "error" ? "Retry install" : "Install Browser"}
                </button>
              )}
            </div>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
