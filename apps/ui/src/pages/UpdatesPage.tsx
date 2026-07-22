import { Dialog } from "@base-ui-components/react/dialog";
import { ExternalLink, RefreshCw } from "lucide-react";
import { useEffect, useRef } from "react";
import AppPage, { panel, primaryButton, secondaryButton } from "../features/apps/AppPage";
import { dialogBackdropCls, dialogPopupCls } from "../components/dialogStyles";
import { useCheckUpdates } from "../lib/appsApi";

export default function UpdatesPage() {
  const check = useCheckUpdates();
  const update = check.data;
  const checkedOnOpen = useRef(false);
  useEffect(() => {
    if (checkedOnOpen.current) return;
    checkedOnOpen.current = true;
    check.mutate();
  }, [check]);
  return (
    <AppPage title="Updates" description="Check published releases from hyudryu/Native-GPT." icon={RefreshCw} actions={<button type="button" className={primaryButton} disabled={check.isPending} onClick={() => check.mutate()}><RefreshCw className={`size-4 ${check.isPending ? "animate-spin" : ""}`} aria-hidden />{check.isPending ? "Checking…" : "Check for updates"}</button>}>
      <section className={panel}><dl className="grid gap-4 sm:grid-cols-2"><div><dt className="text-xs uppercase tracking-wide text-fg-subtle">Installed version</dt><dd className="mt-1 text-xl font-semibold">{update?.current_version ?? "Check to detect"}</dd></div><div><dt className="text-xs uppercase tracking-wide text-fg-subtle">Latest release</dt><dd className="mt-1 text-xl font-semibold">{update?.latest_version ?? "—"}</dd></div></dl>{update && <p className="mt-5 rounded-xl bg-surface-2 p-3 text-sm text-fg-muted">{update.message}</p>}{check.isError && <p role="alert" className="mt-5 rounded-xl bg-danger-subtle p-3 text-sm text-danger">{check.error.message}</p>}</section>
      <p className="mt-4 text-sm text-fg-subtle">Updates are never installed silently. Native GPT opens the selected GitHub release so you can review and install it.</p>
      <section className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-surface-1 p-4 shadow-sm">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">Repository</h2>
          <p className="mt-0.5 truncate text-xs text-fg-subtle">Source code, issues, and releases on GitHub.</p>
        </div>
        <a href="https://github.com/hyudryu/Native-GPT" target="_blank" rel="noreferrer" className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-border bg-surface-1 px-4 text-sm font-medium text-fg-muted hover:bg-surface-2 hover:text-fg">
          hyudryu/Native-GPT
          <ExternalLink className="size-4" aria-hidden />
        </a>
      </section>
      <Dialog.Root open={Boolean(update?.update_available)} onOpenChange={(open) => { if (!open) check.reset(); }}><Dialog.Portal><Dialog.Backdrop className={dialogBackdropCls} /><Dialog.Popup className={dialogPopupCls}><div className="p-5"><Dialog.Title className="text-lg font-semibold">Update Native GPT?</Dialog.Title><Dialog.Description className="mt-2 text-sm leading-6 text-fg-muted">Update from version {update?.current_version} to version {update?.latest_version}?</Dialog.Description>{update?.release_name && <p className="mt-4 font-medium">{update.release_name}</p>}<div className="mt-5 flex justify-end gap-2"><Dialog.Close className={secondaryButton}>No</Dialog.Close><a href={update?.release_url} target="_blank" rel="noreferrer" onClick={() => check.reset()} className={primaryButton}>Yes, open release<ExternalLink className="size-4" aria-hidden /></a></div></div></Dialog.Popup></Dialog.Portal></Dialog.Root>
    </AppPage>
  );
}
