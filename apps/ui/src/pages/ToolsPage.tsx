import { useState } from "react";
import { useNavigate } from "react-router";
import { RotateCcw, ShieldAlert, Store, Wrench } from "lucide-react";
import AppPage, { panel, primaryButton, secondaryButton } from "../features/apps/AppPage";
import { useRollbackTool, useTools, useUpdateTool, type ToolInfo } from "../lib/appsApi";

const badge = "rounded-md bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium text-fg-muted";

const RISK_LABELS: Record<string, string> = {
  read: "Read-only",
  write: "Writes files",
  execute: "Executes code",
  external_side_effect: "External side effects",
};

function ToolBadges({ tool }: { tool: ToolInfo }) {
  if (!tool.risk && !tool.requires_approval && !tool.network) return null;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      {tool.risk && <span className={badge}>{RISK_LABELS[tool.risk] ?? tool.risk}</span>}
      {tool.requires_approval && (
        <span className="inline-flex items-center gap-1 rounded-md bg-warning-subtle px-1.5 py-0.5 text-[11px] font-medium text-warning">
          <ShieldAlert className="size-3" aria-hidden /> Approval required
        </span>
      )}
      {tool.network === "none" && <span className={badge}>No network</span>}
      {tool.timeout_seconds != null && <span className={badge}>{tool.timeout_seconds}s timeout</span>}
    </div>
  );
}

export default function ToolsPage() {
  const tools = useTools();
  const update = useUpdateTool();
  const rollback = useRollbackTool();
  const navigate = useNavigate();
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const handleRollback = (id: string) => {
    setRollbackError(null);
    rollback.mutate(id, {
      onError: (e) => setRollbackError(e.message),
    });
  };
  return (
    <AppPage
      title="Tools"
      description="Manage trusted Strands tools isolated under /tools/<tool-name>."
      icon={Wrench}
      actions={
        <>
          <button type="button" disabled className={secondaryButton} title="Marketplace support is coming later">
            <Store className="size-4" aria-hidden />Browse marketplace · soon
          </button>
          <button type="button" className={primaryButton} onClick={() => navigate("/apps/tools/factory")}>
            <Wrench className="size-4" aria-hidden />Tool Manager
          </button>
        </>
      }
    >
      {tools.isError && <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">{tools.error.message}</p>}
      {rollbackError && <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">{rollbackError}</p>}
      <div className="grid gap-4 md:grid-cols-2">
        {tools.data?.tools.map((tool) => (
          <article key={tool.id} className={panel}>
            <div className="min-w-0">
              <div className="flex items-center justify-between gap-3">
                <h2 className="font-medium">{tool.name}</h2>
                <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-fg-muted">
                  <span>{tool.enabled ? "Enabled" : "Disabled"}</span>
                  <input type="checkbox" className="size-5 accent-[var(--color-accent)]" checked={tool.enabled} disabled={!tool.trusted || update.isPending} onChange={(event) => update.mutate({ id: tool.id, enabled: event.target.checked })} />
                </label>
              </div>
              <p className="mt-1 text-sm text-fg-muted">{tool.description}</p>
              <ToolBadges tool={tool} />
              <p className="mt-3 font-mono text-xs text-fg-subtle">/{tool.folder}/ · v{tool.version}</p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button type="button" className={secondaryButton} onClick={() => navigate(`/apps/tools/factory/${tool.id}`)}>Edit</button>
                {tool.factory_default && (
                  <button type="button" className={secondaryButton} disabled={rollback.isPending} title="Reset this tool to the version that shipped with the app" onClick={() => handleRollback(tool.id)}>
                    <RotateCcw className="size-4" aria-hidden /> Reset to default
                  </button>
                )}
              </div>
            </div>
          </article>
        ))}
        {tools.data?.tools.length === 0 && <p className="text-sm text-fg-muted">No tool folders were discovered.</p>}
      </div>
      <section className={`${panel} mt-4`}><h2 className="text-lg font-medium">Folder isolation</h2><p className="mt-2 text-sm leading-6 text-fg-muted">Each tool owns its code and downloaded assets inside a dedicated <code>/tools/&lt;tool-name&gt;/</code> folder. Only tools marked trusted in their manifest can be enabled.</p></section>
    </AppPage>
  );
}
