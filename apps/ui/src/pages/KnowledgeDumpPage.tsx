import { useCallback, useRef, useState, type ChangeEvent, type DragEvent, type FormEvent } from "react";
import { DatabaseZap, FileUp, Link2, Trash2, UploadCloud } from "lucide-react";
import AppPage, { field, panel, primaryButton } from "../features/apps/AppPage";
import { useDeleteKnowledge, useIngestKnowledge, useKnowledge } from "../lib/appsApi";

const ACCEPTED_TEXT = ".txt,.md,.html,.htm,.json,.csv,.log,text/*";
const MAX_BYTES = 2 * 1024 * 1024; // 2 MB — matches the backend limit.

function isPdf(file: File): boolean {
  return (
    file.type === "application/pdf" ||
    file.name.toLowerCase().endsWith(".pdf")
  );
}

/** Read a file as base64 (without the data: prefix). */
function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("File read failed"));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("File read failed"));
        return;
      }
      // strip the "data:<mime>;base64," prefix
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

export default function KnowledgeDumpPage() {
  const knowledge = useKnowledge();
  const ingest = useIngestKnowledge();
  const remove = useDeleteKnowledge();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [url, setUrl] = useState("");
  const [dragging, setDragging] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function submitPaste(event: FormEvent) { event.preventDefault(); ingest.mutate({ title: title.trim(), source_type: "paste", content }, { onSuccess: () => { setTitle(""); setContent(""); } }); }
  function submitUrl(event: FormEvent) { event.preventDefault(); ingest.mutate({ title: title.trim() || url, source_type: "url", source_uri: url }, { onSuccess: () => { setTitle(""); setUrl(""); } }); }

  const handleFile = useCallback(async (file: File) => {
    setUploadError(null);
    if (file.size > MAX_BYTES) {
      setUploadError(`“${file.name}” is larger than 2 MB.`);
      return;
    }
    try {
      if (isPdf(file)) {
        // Binary — send base64 so the backend can extract the text.
        const contentB64 = await readAsBase64(file);
        ingest.mutate({ title: file.name, source_type: "file", source_uri: file.name, content_b64: contentB64 });
      } else {
        const text = await file.text();
        ingest.mutate({ title: file.name, source_type: "file", source_uri: file.name, content: text });
      }
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "File could not be read.");
    }
  }, [ingest]);

  function onInputChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (file) void handleFile(file);
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLElement>) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void handleFile(file);
  }

  // Only react to drags carrying files (not text/element drags).
  function hasFiles(event: DragEvent<HTMLElement>) {
    return event.dataTransfer.types.includes("Files");
  }

  return (
    <AppPage title="Knowledge Dump" description="Vectorize files, URLs, and pasted notes into app-wide RAG." icon={DatabaseZap}>
      {(ingest.isError || remove.isError || uploadError) && <p role="alert" className="mb-4 rounded-xl bg-danger-subtle p-3 text-sm text-danger">{uploadError ?? ingest.error?.message ?? remove.error?.message}</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <form className={panel} onSubmit={submitPaste}><h2 className="text-lg font-medium">Paste content</h2><input className={`${field} mt-4`} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Knowledge title" /><textarea className="mt-3 min-h-40 w-full resize-y rounded-xl border border-border bg-surface-1 px-3 py-2 text-sm text-fg outline-none focus:border-accent" value={content} onChange={(event) => setContent(event.target.value)} placeholder="Paste notes, documentation, or reference material…" /><button className={`${primaryButton} mt-3`} disabled={!title.trim() || !content.trim() || ingest.isPending}>Vectorize and add</button></form>
        <div className="space-y-4">
          <form className={panel} onSubmit={submitUrl}><h2 className="flex items-center gap-2 text-lg font-medium"><Link2 className="size-5" aria-hidden />Import URL</h2><input type="url" className={`${field} mt-4`} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/docs" /><button className={`${primaryButton} mt-3`} disabled={!url.trim() || ingest.isPending}>Fetch and vectorize</button></form>
          <section
            className={`${panel} relative transition-colors ${dragging ? "border-accent bg-accent-subtle" : ""}`}
            data-drop-zone
            onDragEnter={(event) => { if (hasFiles(event)) { event.preventDefault(); setDragging(true); } }}
            onDragOver={(event) => { if (hasFiles(event)) event.preventDefault(); }}
            onDragLeave={(event) => { if (hasFiles(event) && event.currentTarget === event.target) setDragging(false); }}
            onDrop={onDrop}
          >
            <h2 className="flex items-center gap-2 text-lg font-medium"><FileUp className="size-5" aria-hidden />Upload file</h2>
            <p className="mt-2 text-sm text-fg-muted">Text, Markdown, HTML, JSON, CSV, log, and PDF files up to 2 MB.</p>
            {/* Drop target surface — clickable to open the picker. */}
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className={`mt-4 flex w-full flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-4 py-8 text-sm transition-colors ${dragging ? "border-accent text-accent" : "border-border text-fg-muted hover:bg-surface-2 hover:text-fg"}`}
            >
              <UploadCloud className="size-8" aria-hidden />
              <span>{dragging ? "Drop to upload" : "Drag & drop a file here, or click to browse"}</span>
            </button>
            <input ref={fileInputRef} type="file" accept={`${ACCEPTED_TEXT},.pdf,application/pdf`} className="sr-only" onChange={onInputChange} />
            {ingest.isPending && <p className="mt-3 text-xs text-fg-muted">Processing…</p>}
          </section>
        </div>
      </div>
      <section className={`${panel} mt-4`}><h2 className="text-lg font-medium">Stored knowledge</h2><div className="mt-3 divide-y divide-border">{knowledge.data?.sources.map((source) => <div key={source.id} className="flex items-center gap-3 py-3"><div className="min-w-0 flex-1"><p className="truncate font-medium">{source.title}</p><p className="text-xs text-fg-subtle">{source.source_type} · {source.chunk_count} vector chunks</p></div><button type="button" onClick={() => { if (window.confirm(`Remove “${source.title}” from app-wide knowledge?`)) remove.mutate(source.id); }} className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-danger hover:bg-danger-subtle" aria-label={`Delete ${source.title}`}><Trash2 className="size-4" aria-hidden /></button></div>)}{knowledge.data?.sources.length === 0 && <p className="py-4 text-sm text-fg-muted">No knowledge has been added yet.</p>}</div></section>
    </AppPage>
  );
}
