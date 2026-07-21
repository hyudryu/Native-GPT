import { useState } from "react";
import { Brain, Search } from "lucide-react";
import AppPage, { field, panel } from "../features/apps/AppPage";
import { useKnowledge, useKnowledgeSearch } from "../lib/appsApi";

export default function BrainPage() {
  const [query, setQuery] = useState("");
  const knowledge = useKnowledge();
  const search = useKnowledgeSearch(query);
  return (
    <AppPage title="Brain" description="Explore the knowledge available to every agent and chat." icon={Brain}>
      <div className="grid gap-3 sm:grid-cols-2"><div className={panel}><p className="text-xs uppercase tracking-wide text-fg-subtle">Sources</p><p className="mt-2 text-3xl font-semibold">{knowledge.data?.stats.source_count ?? "—"}</p></div><div className={panel}><p className="text-xs uppercase tracking-wide text-fg-subtle">Vector chunks</p><p className="mt-2 text-3xl font-semibold">{knowledge.data?.stats.chunk_count ?? "—"}</p></div></div>
      <section className={`${panel} mt-4`}><label htmlFor="brain-search" className="text-sm font-medium">Search app-wide knowledge</label><div className="relative mt-2"><Search className="absolute left-3 top-3 size-5 text-fg-subtle" aria-hidden /><input id="brain-search" className={`${field} pl-10`} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ask what the Brain knows…" /></div>
        <div className="mt-4 space-y-3">{search.data?.matches.map((match) => <article key={match.chunk_id} className="rounded-xl bg-surface-2 p-4"><div className="flex justify-between gap-3"><h3 className="font-medium">{match.source_title}</h3><span className="text-xs text-fg-subtle">{Math.round(match.score * 100)}% match</span></div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-fg-muted">{match.content}</p></article>)}{query.length > 1 && !search.isPending && search.data?.matches.length === 0 && <p className="text-sm text-fg-muted">No related knowledge found.</p>}</div>
      </section>
      <section className={`${panel} mt-4`}><h2 className="text-lg font-medium">Knowledge sources</h2><div className="mt-3 divide-y divide-border">{knowledge.data?.sources.map((source) => <div key={source.id} className="flex items-center justify-between gap-3 py-3"><div><p className="font-medium">{source.title}</p><p className="text-xs text-fg-subtle">{source.source_type} · {source.chunk_count} chunks</p></div></div>)}{knowledge.data?.sources.length === 0 && <p className="py-3 text-sm text-fg-muted">The Brain is empty. Add content in Knowledge Dump.</p>}</div></section>
    </AppPage>
  );
}
