import { useState, type ChangeEvent, type FormEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { FileUp, Folder, Link2, MessageSquare, Plus, Trash2 } from "lucide-react";
import { field, panel, primaryButton, secondaryButton } from "../features/apps/AppPage";
import {
  useCreateConversation,
  useDeleteConversation,
  useProject,
  useProjectConversations,
  useUpdateConversation,
} from "../lib/dataApi";
import { useDeleteKnowledge, useIngestKnowledge, useKnowledge } from "../lib/appsApi";
import { relativeTime } from "../lib/relTime";

type Tab = "chats" | "sources";

export default function ProjectPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const tab: Tab = tabParam === "sources" ? "sources" : "chats";
  const setTab = (next: Tab) => setSearchParams(next === "chats" ? {} : { tab: next }, { replace: true });

  const project = useProject(projectId);
  const conversations = useProjectConversations(projectId);

  if (project.isError) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="text-center">
          <Folder className="mx-auto size-10 text-fg-subtle" aria-hidden />
          <p className="mt-3 text-sm text-fg-muted">This project could not be loaded.</p>
          <button type="button" onClick={() => navigate("/")} className="mt-4 text-sm text-accent hover:underline">
            Back to home
          </button>
        </div>
      </div>
    );
  }

  const tabButton = (value: Tab, label: string) => (
    <button
      type="button"
      onClick={() => setTab(value)}
      className={`min-h-11 rounded-xl px-4 text-sm font-medium transition-colors ${
        tab === value ? "bg-surface-2 text-fg" : "text-fg-muted hover:bg-surface-2 hover:text-fg"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="h-full min-h-0 overflow-y-auto overscroll-contain">
      <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6">
        <header className="flex flex-wrap items-center gap-4">
          <span className="flex size-12 items-center justify-center rounded-2xl bg-accent text-white">
            <Folder className="size-6" aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl font-semibold tracking-tight">
              {project.isPending ? "Loading…" : project.data?.name ?? "Project"}
            </h1>
            <p className="mt-1 text-sm text-fg-muted">
              {project.data?.instructions?.trim()
                ? project.data.instructions
                : "Conversations and RAG sources scoped to this project."}
            </p>
          </div>
        </header>

        <div className="mt-6 inline-flex gap-1 rounded-2xl border border-border bg-surface-1 p-1">
          {tabButton("chats", "Chats")}
          {tabButton("sources", "Sources")}
        </div>

        <div className="mt-6">
          {tab === "chats" ? (
            <ChatsTab projectId={projectId} conversations={conversations.data ?? []} loading={conversations.isPending} />
          ) : (
            <SourcesTab projectId={projectId} />
          )}
        </div>
      </div>
    </div>
  );
}

function ChatsTab({
  projectId,
  conversations,
  loading,
}: {
  projectId: string;
  conversations: { id: string; title: string; updated_at?: string; created_at?: string; archived_at?: string | null }[];
  loading: boolean;
}) {
  const navigate = useNavigate();
  const createConversation = useCreateConversation();
  const updateConversation = useUpdateConversation();
  const deleteConversation = useDeleteConversation();
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const active = conversations.filter((item) => !item.archived_at);

  const create = () => {
    createConversation.mutate(
      { title: "New conversation", project_id: projectId },
      { onSuccess: (item) => navigate(`/conversations/${item.id}`) },
    );
  };

  return (
    <section className={panel}>
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium">Conversations</h2>
        <button type="button" onClick={create} disabled={createConversation.isPending} className={primaryButton}>
          <Plus className="size-4" aria-hidden /> New conversation
        </button>
      </div>

      {(createConversation.isError || deleteConversation.isError || updateConversation.isError) && (
        <p role="alert" className="mt-3 rounded-xl bg-danger-subtle p-3 text-sm text-danger">
          {createConversation.error?.message ?? deleteConversation.error?.message ?? updateConversation.error?.message}
        </p>
      )}

      {loading ? (
        <p className="mt-4 text-sm text-fg-muted">Loading conversations…</p>
      ) : active.length === 0 ? (
        <p className="mt-4 text-sm text-fg-muted">No conversations in this project yet.</p>
      ) : (
        <div className="mt-3 divide-y divide-border">
          {active.map((conversation) => (
            <div key={conversation.id} className="flex items-center gap-3 py-3">
              <button
                type="button"
                onClick={() => navigate(`/conversations/${conversation.id}`)}
                className="flex min-w-0 flex-1 items-center gap-2 text-left"
              >
                <MessageSquare className="size-4 shrink-0 text-fg-subtle" aria-hidden />
                <span className="min-w-0">
                  <span className="block truncate font-medium">{conversation.title}</span>
                  <span className="block truncate text-xs text-fg-subtle">
                    {relativeTime(conversation.updated_at ?? conversation.created_at ?? "")}
                  </span>
                </span>
              </button>
              {confirmingId === conversation.id ? (
                <span className="flex items-center gap-1 text-xs">
                  <button
                    type="button"
                    onClick={() => {
                      deleteConversation.mutate(conversation.id);
                      setConfirmingId(null);
                    }}
                    className="text-danger hover:underline"
                  >
                    Delete
                  </button>
                  <button type="button" onClick={() => setConfirmingId(null)} className="text-fg-muted hover:underline">
                    Cancel
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmingId(conversation.id)}
                  className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-danger hover:bg-danger-subtle"
                  aria-label={`Delete ${conversation.title}`}
                >
                  <Trash2 className="size-4" aria-hidden />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SourcesTab({ projectId }: { projectId: string }) {
  const knowledge = useKnowledge(projectId);
  const ingest = useIngestKnowledge(projectId);
  const remove = useDeleteKnowledge(projectId);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [url, setUrl] = useState("");

  function submitPaste(event: FormEvent) {
    event.preventDefault();
    ingest.mutate(
      { title: title.trim(), source_type: "paste", content },
      { onSuccess: () => { setTitle(""); setContent(""); } },
    );
  }
  function submitUrl(event: FormEvent) {
    event.preventDefault();
    ingest.mutate(
      { title: title.trim() || url, source_type: "url", source_uri: url },
      { onSuccess: () => { setTitle(""); setUrl(""); } },
    );
  }
  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    ingest.mutate({ title: file.name, source_type: "file", source_uri: file.name, content: text });
    event.target.value = "";
  }

  return (
    <div className="space-y-4">
      {(ingest.isError || remove.isError) && (
        <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">
          {ingest.error?.message ?? remove.error?.message}
        </p>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <form className={panel} onSubmit={submitPaste}>
          <h2 className="text-lg font-medium">Paste content</h2>
          <input className={`${field} mt-4`} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Knowledge title" />
          <textarea
            className="mt-3 min-h-40 w-full resize-y rounded-xl border border-border bg-surface-1 px-3 py-2 text-sm text-fg outline-none focus:border-accent"
            value={content}
            onChange={(event) => setContent(event.target.value)}
            placeholder="Paste notes, documentation, or reference material…"
          />
          <button className={`${primaryButton} mt-3`} disabled={!title.trim() || !content.trim() || ingest.isPending}>
            Vectorize and add
          </button>
        </form>
        <div className="space-y-4">
          <form className={panel} onSubmit={submitUrl}>
            <h2 className="flex items-center gap-2 text-lg font-medium">
              <Link2 className="size-5" aria-hidden />Import URL
            </h2>
            <input type="url" className={`${field} mt-4`} value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/docs" />
            <button className={`${primaryButton} mt-3`} disabled={!url.trim() || ingest.isPending}>
              Fetch and vectorize
            </button>
          </form>
          <section className={panel}>
            <h2 className="flex items-center gap-2 text-lg font-medium">
              <FileUp className="size-5" aria-hidden />Upload text file
            </h2>
            <p className="mt-2 text-sm text-fg-muted">Text, Markdown, HTML, JSON, CSV, and log files up to 2 MB.</p>
            <label className={`${secondaryButton} mt-4 cursor-pointer`}>
              <FileUp className="size-4" aria-hidden />Choose file
              <input type="file" accept=".txt,.md,.html,.htm,.json,.csv,.log,text/*" className="sr-only" onChange={upload} />
            </label>
          </section>
        </div>
      </div>
      <section className={panel}>
        <h2 className="text-lg font-medium">Project sources</h2>
        <p className="mt-1 text-sm text-fg-muted">
          Sources here are scoped to this project. Global knowledge still applies to these chats.
        </p>
        <div className="mt-3 divide-y divide-border">
          {knowledge.data?.sources.map((source) => (
            <div key={source.id} className="flex items-center gap-3 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{source.title}</p>
                <p className="text-xs text-fg-subtle">
                  {source.source_type} · {source.chunk_count} vector chunks
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (window.confirm(`Remove "${source.title}" from this project?`)) remove.mutate(source.id);
                }}
                className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-danger hover:bg-danger-subtle"
                aria-label={`Delete ${source.title}`}
              >
                <Trash2 className="size-4" aria-hidden />
              </button>
            </div>
          ))}
          {knowledge.data?.sources.length === 0 && (
            <p className="py-4 text-sm text-fg-muted">No sources scoped to this project yet.</p>
          )}
        </div>
      </section>
    </div>
  );
}
