import { Dialog } from "@base-ui-components/react/dialog";
import { ShieldAlert } from "lucide-react";
import {
  dialogBackdropCls,
  dialogPopupCls,
} from "../../components/dialogStyles";
import { useResolveBrowserApproval } from "./browserApi";
import { useBrowserStore } from "./browserStore";
import type { PermissionCapability } from "./types";

const CAPABILITY_LABELS: Record<string, string> = {
  navigate_public_web: "Navigate pages on the public web",
  navigate_private_network: "Navigate local / private-network sites",
  upload_file: "Upload files to the page",
  download_file: "Download files",
  submit_form: "Submit a form",
  send_message: "Send a message",
  publish_content: "Publish content",
  delete_content: "Delete content",
  financial_transaction: "Make a purchase or payment",
  credential_entry: "Enter credentials",
};

function capabilityLabel(capability: PermissionCapability): string {
  return CAPABILITY_LABELS[capability] ?? capability.replaceAll("_", " ");
}

/**
 * Approval gate for sensitive browser actions (spec §11.2). Driven by
 * `pendingApprovals` in the browser state; resolves through
 * `POST /api/browser/approvals/{id}/resolve`. File-upload approvals show the
 * exact filenames and destination origin from the request description.
 */
export default function BrowserPermissionDialog() {
  const approvals = useBrowserStore((s) => s.pendingApprovals);
  const resolve = useResolveBrowserApproval();

  const approval = approvals[0];
  const open = approvals.length > 0;

  const answer = (allow: boolean, scope?: "once" | "conversation") => {
    if (!approval) return;
    resolve.mutate({ id: approval.id, allow, scope });
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={() => {
        /* modal: resolved only through the buttons below */
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className={dialogBackdropCls} />
        <Dialog.Popup className={dialogPopupCls}>
          <div className="p-5">
            <div className="flex items-center gap-2">
              <ShieldAlert className="size-5 text-accent" aria-hidden />
              <Dialog.Title className="text-lg font-semibold">
                Native GPT wants to control the browser
              </Dialog.Title>
            </div>
            {approval && (
              <>
                <Dialog.Description className="mt-3 text-sm text-fg-muted">
                  {approval.description}
                </Dialog.Description>
                <div className="mt-3 rounded-xl border border-border bg-surface-1 p-3 text-sm">
                  <p className="text-fg">
                    <span className="text-fg-muted">Requested: </span>
                    {capabilityLabel(approval.capability)}
                  </p>
                  {approval.origin && (
                    <p className="mt-1 break-all text-fg">
                      <span className="text-fg-muted">Origin: </span>
                      {approval.origin}
                    </p>
                  )}
                </div>
                {approvals.length > 1 && (
                  <p className="mt-2 text-xs text-fg-subtle">
                    {approvals.length - 1} more approval
                    {approvals.length > 2 ? "s" : ""} pending.
                  </p>
                )}
                {resolve.isError && (
                  <p role="alert" className="mt-2 text-sm text-danger">
                    {resolve.error.message}
                  </p>
                )}
                <div className="mt-5 flex flex-col gap-2 sm:flex-row sm:justify-end">
                  <button
                    type="button"
                    onClick={() => answer(false, "once")}
                    disabled={resolve.isPending}
                    className="min-h-11 rounded-xl px-4 text-sm font-medium text-danger hover:bg-danger-subtle disabled:opacity-50"
                  >
                    Deny
                  </button>
                  <button
                    type="button"
                    onClick={() => answer(true, "conversation")}
                    disabled={resolve.isPending}
                    className="min-h-11 rounded-xl border border-border px-4 text-sm font-medium text-fg hover:bg-surface-2 disabled:opacity-50"
                  >
                    Allow for this conversation
                  </button>
                  <button
                    type="button"
                    onClick={() => answer(true, "once")}
                    disabled={resolve.isPending}
                    className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"
                  >
                    Allow once
                  </button>
                </div>
              </>
            )}
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
