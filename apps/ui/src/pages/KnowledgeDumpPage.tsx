import { useState, type ChangeEvent, type FormEvent } from "react";
import { DatabaseZap, FileUp, Link2, Trash2 } from "lucide-react";
import AppPage, { field, panel, primaryButton, secondaryButton } from "../features/apps/AppPage";
import { useDeleteKnowledge, useIngestKnowledge, useKnowledge } from "../lib/appsApi";

export default function KnowledgeDumpPage() {
  const knowledge = useKnowledge();
  const ingest = useIngestKnowledge();
  const remove = useDeleteKnowledge();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [url, setUrl] = useState("");

  function submitPaste(event: FormEvent) { event.preventDefault(); ingest.mutate({ title: title.trim(), source_type: "paste", content }, { onSuccess: () => { setTitle(""); setContent(""); } }); }
  function submitUrl(event: FormEvent) { event.preventDefault(); ingest.mutate({ title: title.trim() || url, source_type: "url", source_uri: url }, { onSuccess: () => { setTitle(""); setUrl(""); } }); }
  async function upload(event: ChangeEvent<HTMLInputElement>) { const file = event.target.files?.[0]; if (!file) return; const text = await file.text(); ingest.mutate({ title: file.name, source_type: "file", source_uri: file.name, content: text }); event.target.value = ""; }

  return (
    <AppPage title="Knowledge Dump" description="Vectorize files, URLs, and pasted notes into app-wide RAG." icon={DatabaseZap}>
      {(ingest.isError || remove.isError) && <p role="alert" className="mb-4 rounded-xl bg-danger-subtle p-3 text-sm text-danger">{ingest.error?.message ?? remove.error?.message}</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <form className={panel} onSubmit={submitPaste}><h2 className="text-lg font-medium">Paste content</h2><input className={`${field} mt-4`} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Knowledge title" /><textarea className="mt-3 min-h-40 w-full resize-y rounded-xl border border-border bg-surface-1 px-3 py-2 text-sm text-fg outline-none focus:border-accent" value={content} onChange={(event) => setContent(event.target.value)} placeholder="Paste notes, documentation, or reference material…" /><button className={`${primaryButton} mt-3`} disabled={!title.trim() || !content.trim() || ingest.isPending}>Vectorize and add</button></form>
        <div className="space-y-4"><form className={panel} onSubmit={submitUrl}><h2 className="flex items-center gap-2 text-lg font-medium"><Link2 className="size-5" aria-hidden />Import URL</h2><input type="url" className={`${field} mt-4`} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/docs" /><button className={`${primaryButton} mt-3`} disabled={!url.trim() || ingest.isPending}>Fetch and vectorize</button></form><section className={panel}><h2 className="flex items-center gap-2 text-lg font-medium"><FileUp className="size-5" aria-hidden />Upload text file</h2><p className="mt-2 text-sm text-fg-muted">Text, Markdown, HTML, JSON, CSV, and log files up to 2 MB.</p><label className={`${secondaryButton} mt-4 cursor-pointer`}><FileUp className="size-4" aria-hidden />Choose file<input type="file" accept=".txt,.md,.html,.htm,.json,.csv,.log,text/*" className="sr-only" onChange={upload} /></label></section></div>
      </div>
      <section className={`${panel} mt-4`}><h2 className="text-lg font-medium">Stored knowledge</h2><div className="mt-3 divide-y divide-border">{knowledge.data?.sources.map((source) => <div key={source.id} className="flex items-center gap-3 py-3"><div className="min-w-0 flex-1"><p className="truncate font-medium">{source.title}</p><p className="text-xs text-fg-subtle">{source.source_type} · {source.chunk_count} vector chunks</p></div><button type="button" onClick={() => { if (window.confirm(`Remove “${source.title}” from app-wide knowledge?`)) remove.mutate(source.id); }} className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-danger hover:bg-danger-subtle" aria-label={`Delete ${source.title}`}><Trash2 className="size-4" aria-hidden /></button></div>)}{knowledge.data?.sources.length === 0 && <p className="py-4 text-sm text-fg-muted">No knowledge has been added yet.</p>}</div></section>
    </AppPage>
  );
}
