import { Store, Wrench } from "lucide-react";
import AppPage, { panel, secondaryButton } from "../features/apps/AppPage";
import { useTools, useUpdateTool } from "../lib/appsApi";

export default function ToolsPage() {
  const tools = useTools();
  const update = useUpdateTool();
  return (
    <AppPage title="Tools" description="Manage trusted Strands tools isolated under /tools/<tool-name>." icon={Wrench} actions={<button type="button" disabled className={secondaryButton} title="Marketplace support is coming later"><Store className="size-4" aria-hidden />Browse marketplace · soon</button>}>
      {tools.isError && <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">{tools.error.message}</p>}
      <div className="grid gap-4 md:grid-cols-2">{tools.data?.tools.map((tool) => <article key={tool.id} className={panel}><div className="flex items-start gap-3"><span className="flex size-10 items-center justify-center rounded-xl bg-accent text-white"><Wrench className="size-5" aria-hidden /></span><div className="min-w-0 flex-1"><div className="flex items-center justify-between gap-3"><h2 className="font-medium">{tool.name}</h2><label className="inline-flex cursor-pointer items-center gap-2 text-xs text-fg-muted"><span>{tool.enabled ? "Enabled" : "Disabled"}</span><input type="checkbox" className="size-5 accent-[var(--color-accent)]" checked={tool.enabled} disabled={!tool.trusted || update.isPending} onChange={(event) => update.mutate({ id: tool.id, enabled: event.target.checked })} /></label></div><p className="mt-1 text-sm text-fg-muted">{tool.description}</p><p className="mt-3 font-mono text-xs text-fg-subtle">/{tool.folder}/ · v{tool.version}</p></div></div></article>)}{tools.data?.tools.length === 0 && <p className="text-sm text-fg-muted">No tool folders were discovered.</p>}</div>
      <section className={`${panel} mt-4`}><h2 className="text-lg font-medium">Folder isolation</h2><p className="mt-2 text-sm leading-6 text-fg-muted">Each tool owns its code and downloaded assets inside a dedicated <code>/tools/&lt;tool-name&gt;/</code> folder. Only tools marked trusted in their manifest can be enabled.</p></section>
    </AppPage>
  );
}
