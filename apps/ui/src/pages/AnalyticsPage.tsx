import { BarChart3 } from "lucide-react";
import AppPage, { panel } from "../features/apps/AppPage";
import { useAnalytics } from "../lib/appsApi";

const number = new Intl.NumberFormat();

export default function AnalyticsPage() {
  const analytics = useAnalytics();
  const totals = analytics.data?.totals;
  return (
    <AppPage title="Analytics" description="Usage collected from completed model runs." icon={BarChart3}>
      {analytics.isError && <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">{analytics.error.message}</p>}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[["Runs", totals?.runs], ["Input tokens", totals?.input_tokens], ["Output tokens", totals?.output_tokens], ["Average tok/s", totals?.average_tokens_per_second?.toFixed(1)]].map(([label, value]) => (
          <div key={label} className={panel}><p className="text-xs uppercase tracking-wide text-fg-subtle">{label}</p><p className="mt-2 text-2xl font-semibold">{typeof value === "number" ? number.format(value) : value ?? "—"}</p></div>
        ))}
      </div>
      <section className={`${panel} mt-4 overflow-x-auto`}>
        <h2 className="text-lg font-medium">By model</h2>
        {analytics.isPending ? <p className="mt-3 text-sm text-fg-muted">Loading usage…</p> : analytics.data?.models.length ? (
          <table className="mt-4 w-full min-w-[720px] text-left text-sm"><thead className="text-xs uppercase text-fg-subtle"><tr><th className="pb-3">Provider / model</th><th>Runs</th><th>Input</th><th>Output</th><th>Total</th><th>Tok/s</th><th>Avg duration</th></tr></thead><tbody>{analytics.data.models.map((model) => <tr key={`${model.provider_name}:${model.model_id}`} className="border-t border-border"><td className="py-3"><span className="font-medium">{model.provider_name}</span><span className="block font-mono text-xs text-fg-subtle">{model.model_id}</span></td><td>{model.runs}</td><td>{number.format(model.input_tokens)}</td><td>{number.format(model.output_tokens)}</td><td>{number.format(model.total_tokens)}</td><td>{model.average_tokens_per_second.toFixed(1)}</td><td>{(model.average_run_duration_ms / 1000).toFixed(1)}s</td></tr>)}</tbody></table>
        ) : <p className="mt-3 text-sm text-fg-muted">Analytics will appear after the first completed chat run.</p>}
      </section>
    </AppPage>
  );
}
