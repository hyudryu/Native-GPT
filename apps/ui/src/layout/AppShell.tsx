import { useState } from "react";
import { Dialog } from "@base-ui-components/react/dialog";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import {
  Archive,
  ArchiveRestore,
  ChevronDown,
  Download,
  Ellipsis,
  Folder,
  Grid2X2,
  LoaderCircle,
  Menu,
  MessageSquare,
  PanelLeft,
  Pencil,
  Plus,
  Search,
  Settings,
  SquarePen,
  Trash2,
  X,
} from "lucide-react";
import ThemeToggle from "../components/ThemeToggle";
import FileDropOverlay from "../components/FileDropOverlay";
import WindowTitleBar from "../components/WindowTitleBar";
import AppsMenu from "../features/apps/AppsMenu";
import BrowserPanel from "../features/browser/BrowserPanel";
import BrowserPermissionDialog from "../features/browser/BrowserPermissionDialog";
import { useBrowserStore } from "../features/browser/browserStore";
import { dialogBackdropCls, dialogPopupCls } from "../components/dialogStyles";
import { conversationMarkdown, safeExportName } from "../lib/conversationExport";
import {
  listMessages,
  modelOptionValue,
  parseModelOptionValue,
  useArchivedConversations,
  useConversations,
  useCreateConversation,
  useCreateProject,
  useDeleteConversation,
  useDeleteProject,
  useEnabledModels,
  useProjects,
  useSearch,
  useUpdateConversation,
  useUpdateProject,
  type Conversation,
  type Project,
} from "../lib/dataApi";
import { relativeTime } from "../lib/relTime";
import { useRailModeStore } from "../lib/railMode";

const iconButton =
  "inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg";
const row =
  "flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-xl px-3 text-left text-sm text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg";

function closeDetails(element: HTMLElement) {
  const details = element.closest("details") as HTMLDetailsElement | null;
  if (details) details.open = false;
}

function ConversationMenu({
  conversation,
  onRename,
  onDelete,
  onArchive,
  onExport,
  exporting,
}: {
  conversation: Conversation;
  onRename: () => void;
  onDelete: () => void;
  onArchive: () => void;
  onExport: () => void;
  exporting: boolean;
}) {
  return (
    <details
      className="relative shrink-0"
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.currentTarget.open = false;
        event.currentTarget.querySelector("summary")?.focus();
      }}
    >
      <summary
        aria-label={`Actions for ${conversation.title}`}
        className="flex min-h-11 min-w-9 cursor-pointer list-none items-center justify-center rounded-xl text-fg-subtle hover:bg-surface-2 hover:text-fg [&::-webkit-details-marker]:hidden"
      >
        <Ellipsis className="size-4" aria-hidden />
      </summary>
      {/* Invisible backdrop: clicking outside the menu dismisses it */}
      <div
        className="fixed inset-0 z-20"
        onClick={(e) => {
          e.preventDefault();
          const details = (e.currentTarget as HTMLElement).closest("details");
          if (details) details.open = false;
        }}
      />
      <div
        role="menu"
        aria-label={`Conversation actions for ${conversation.title}`}
        className="absolute right-0 top-10 z-30 w-36 rounded-xl border border-border bg-surface-3 p-1 shadow-lg"
      >
        <button
          type="button"
          role="menuitem"
          onClick={(event) => {
            closeDetails(event.currentTarget);
            onArchive();
          }}
          className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
        >
          <Archive className="size-4" aria-hidden /> Archive
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={(event) => {
            closeDetails(event.currentTarget);
            onRename();
          }}
          className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
        >
          <Pencil className="size-4" aria-hidden /> Rename
        </button>
        <button
          type="button"
          role="menuitem"
          disabled={exporting}
          onClick={(event) => {
            closeDetails(event.currentTarget);
            onExport();
          }}
          className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-fg-muted hover:bg-surface-2 hover:text-fg disabled:opacity-50"
        >
          {exporting ? (
            <LoaderCircle className="size-4 animate-spin" aria-hidden />
          ) : (
            <Download className="size-4" aria-hidden />
          )}
          Export
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={(event) => {
            closeDetails(event.currentTarget);
            onDelete();
          }}
          className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-danger hover:bg-danger-subtle"
        >
          <Trash2 className="size-4" aria-hidden /> Delete
        </button>
      </div>
    </details>
  );
}

function WorkspaceMenu({ onEdit, onDelete, name }: { onEdit: () => void; onDelete: () => void; name: string }) {
  return (
    <details
      className="relative ml-auto shrink-0"
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.currentTarget.open = false;
        event.currentTarget.querySelector("summary")?.focus();
      }}
    >
      <summary aria-label={`Actions for workspace ${name}`} className="flex min-h-9 min-w-9 cursor-pointer list-none items-center justify-center rounded-lg text-fg-subtle hover:bg-surface-2 hover:text-fg [&::-webkit-details-marker]:hidden">
        <Ellipsis className="size-4" aria-hidden />
      </summary>
      {/* Invisible backdrop: clicking outside the menu dismisses it */}
      <div
        className="fixed inset-0 z-20"
        onClick={(e) => {
          e.preventDefault();
          const details = (e.currentTarget as HTMLElement).closest("details");
          if (details) details.open = false;
        }}
      />
      <div role="menu" aria-label={`Workspace actions for ${name}`} className="absolute right-0 top-8 z-30 w-32 rounded-xl border border-border bg-surface-3 p-1 shadow-lg">
        <button type="button" role="menuitem" onClick={(event) => { closeDetails(event.currentTarget); onEdit(); }} className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"><Pencil className="size-4" aria-hidden />Edit</button>
        <button type="button" role="menuitem" onClick={(event) => { closeDetails(event.currentTarget); onDelete(); }} className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-sm text-danger hover:bg-danger-subtle"><Trash2 className="size-4" aria-hidden />Delete</button>
      </div>
    </details>
  );
}

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const navigate = useNavigate();
  const location = useLocation();
  const projects = useProjects();
  const conversations = useConversations();
  const archivedConversations = useArchivedConversations();
  const createConversation = useCreateConversation();
  const createProject = useCreateProject();
  const updateProject = useUpdateProject();
  const deleteProject = useDeleteProject();
  const enabledModels = useEnabledModels();
  const updateConversation = useUpdateConversation();
  const deleteConversation = useDeleteConversation();
  const [query, setQuery] = useState("");
  const search = useSearch(query);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [editingWorkspace, setEditingWorkspace] = useState<Project | null>(null);
  const [workspaceName, setWorkspaceName] = useState("");
  const [workspaceInstructions, setWorkspaceInstructions] = useState("");
  const [workspaceModel, setWorkspaceModel] = useState("");
  const [renaming, setRenaming] = useState<Conversation | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [exportingId, setExportingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const openWorkspaceDialog = (project: Project | null) => {
    setEditingWorkspace(project);
    setWorkspaceName(project?.name ?? "");
    setWorkspaceInstructions(project?.instructions ?? "");
    setWorkspaceModel(
      project?.endpoint_id && project.model_id
        ? modelOptionValue({ provider_id: project.endpoint_id, model_id: project.model_id })
        : "",
    );
    setWorkspaceOpen(true);
  };

  const openConversation = (id: string) => {
    navigate(`/conversations/${id}`);
    onNavigate?.();
  };

  const startConversation = () => {
    // Reuse the most-recent empty conversation instead of creating another.
    // `list_conversations` returns rows ordered by updated_at DESC, so the
    // first empty (non-archived, zero-message) conversation is the newest.
    const empty = (conversations.data ?? []).find(
      (item) => !item.archived_at && (item.message_count ?? 0) === 0,
    );
    onNavigate?.();
    if (empty) {
      navigate(`/conversations/${empty.id}`);
    } else {
      navigate("/");
    }
  };

  const removeConversation = (conversation: Conversation) => {
    if (!window.confirm(`Delete “${conversation.title}”? This cannot be undone.`)) return;
    deleteConversation.mutate(conversation.id, {
      onSuccess: () => {
        if (location.pathname === `/conversations/${conversation.id}`) navigate("/");
      },
    });
  };

  const exportConversation = async (conversation: Conversation) => {
    setActionError(null);
    setExportingId(conversation.id);
    try {
      const messages = await listMessages(conversation.id);
      const blob = new Blob([conversationMarkdown(conversation, messages)], {
        type: "text/markdown;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${safeExportName(conversation.title)}.md`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Conversation export failed.");
    } finally {
      setExportingId(null);
    }
  };

  const conversationRow = (conversation: Conversation) => (
    <li key={conversation.id} className="flex min-w-0 items-center gap-1">
      <NavLink
        to={`/conversations/${conversation.id}`}
        onClick={onNavigate}
        className={({ isActive }) =>
          `${row} ${isActive ? "bg-surface-2 text-fg" : ""}`
        }
      >
        <MessageSquare className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{conversation.title}</span>
      </NavLink>
      <ConversationMenu
        conversation={conversation}
        exporting={exportingId === conversation.id}
        onRename={() => {
          setRenaming(conversation);
          setRenameTitle(conversation.title);
        }}
        onDelete={() => removeConversation(conversation)}
        onArchive={() =>
          updateConversation.mutate(
            { id: conversation.id, input: { archived: true } },
            {
              onSuccess: () => {
                if (location.pathname === `/conversations/${conversation.id}`) navigate("/");
              },
            },
          )
        }
        onExport={() => void exportConversation(conversation)}
      />
    </li>
  );

  const activeConversations = (conversations.data ?? []).filter((item) => !item.archived_at);
  const ungrouped = activeConversations.filter((item) => !item.project_id);
  const archived = archivedConversations.data ?? [];

  const unarchiveConversation = (conversation: Conversation) => {
    updateConversation.mutate({ id: conversation.id, input: { archived: false } });
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface-1">
      <div className="flex items-center gap-1 px-3 py-2">
        <span className="px-1 text-base font-semibold tracking-tight">
          Native <span className="font-normal text-fg-subtle">GPT</span>
        </span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={startConversation}
          aria-label="New conversation"
          className={iconButton}
        >
          <SquarePen className="size-5" aria-hidden />
        </button>
      </div>

      <div className="px-3 pb-2">
        <label className="relative block">
          <span className="sr-only">Search conversations</span>
          <Search className="pointer-events-none absolute left-3 top-3.5 size-4 text-fg-subtle" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search conversations"
            className="min-h-11 w-full rounded-xl border border-border bg-surface-0 pl-9 pr-3 text-sm text-fg placeholder:text-fg-subtle"
          />
        </label>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {query.trim().length >= 2 ? (
          <section aria-labelledby="search-results">
            <h2 id="search-results" className="px-3 py-2 text-xs font-medium uppercase tracking-wide text-fg-subtle">
              Search results
            </h2>
            {search.isPending && <p className="px-3 text-xs text-fg-muted">Searching…</p>}
            {search.isError && <p role="alert" className="px-3 text-xs text-danger">Search failed.</p>}
            {search.data?.length === 0 && <p className="px-3 text-xs text-fg-muted">No matches.</p>}
            <ul className="space-y-0.5">
              {search.data?.map((result) => (
                <li key={result.conversation_id}>
                  <button type="button" onClick={() => openConversation(result.conversation_id)} className={row}>
                    <span className="min-w-0">
                      <span className="block truncate text-fg">{result.title}</span>
                      {result.snippet && <span className="block truncate text-xs text-fg-subtle">{result.snippet}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : (
          <>
            <AppsMenu onNavigate={onNavigate} />
            <section aria-labelledby="workspaces-heading">
              <div className="flex items-center justify-between px-3 py-2">
                <h2 id="workspaces-heading" className="text-xs font-medium uppercase tracking-wide text-fg-subtle">
                  Workspaces
                </h2>
                <button
                  type="button"
                  onClick={() => openWorkspaceDialog(null)}
                  aria-label="New workspace"
                  className="inline-flex min-h-9 min-w-9 items-center justify-center rounded-lg text-fg-subtle hover:bg-surface-2 hover:text-fg"
                >
                  <Plus className="size-4" aria-hidden />
                </button>
              </div>
              {projects.isPending && <p className="px-3 text-xs text-fg-muted">Loading workspaces…</p>}
              <ul className="space-y-2">
                {projects.data?.map((project) => {
                  const items = activeConversations.filter((item) => item.project_id === project.id);
                  return (
                    <li key={project.id}>
                      <div className="flex min-h-10 items-center gap-2 px-3 text-sm font-medium text-fg-muted">
                        <Folder className="size-4 shrink-0" aria-hidden />
                        <NavLink
                          to={`/projects/${project.id}`}
                          onClick={onNavigate}
                          className="min-w-0 flex-1 truncate rounded-lg px-1 py-1 text-left transition-colors hover:bg-surface-2 hover:text-fg"
                        >
                          {project.name}
                        </NavLink>
                        <span className="text-xs text-fg-subtle">{items.length}</span>
                        <button
                          type="button"
                          aria-label={`New conversation in ${project.name}`}
                          onClick={() =>
                            createConversation.mutate(
                              { title: "New conversation", project_id: project.id },
                              { onSuccess: (item) => openConversation(item.id) },
                            )
                          }
                          className="inline-flex min-h-9 min-w-9 items-center justify-center rounded-lg text-fg-subtle hover:bg-surface-2 hover:text-fg"
                        >
                          <Plus className="size-4" aria-hidden />
                        </button>
                        <WorkspaceMenu
                          name={project.name}
                          onEdit={() => openWorkspaceDialog(project)}
                          onDelete={() => {
                            if (!window.confirm(`Delete workspace “${project.name}”? Conversations will remain ungrouped.`)) return;
                            deleteProject.mutate(project.id);
                          }}
                        />
                      </div>
                      {items.length > 0 && <ul className="ml-3 space-y-0.5">{items.map(conversationRow)}</ul>}
                    </li>
                  );
                })}
              </ul>
            </section>

            <section aria-labelledby="chats-heading" className="mt-4">
              <h2 id="chats-heading" className="px-3 py-2 text-xs font-medium uppercase tracking-wide text-fg-subtle">
                Chats
              </h2>
              {conversations.isPending && <p className="px-3 text-xs text-fg-muted">Loading chats…</p>}
              {(conversations.isError || projects.isError) && (
                <p role="alert" className="px-3 text-xs text-danger">Navigation data could not be loaded.</p>
              )}
              <ul className="space-y-0.5">{ungrouped.map(conversationRow)}</ul>
            </section>

            {archived.length > 0 && (
              <section aria-labelledby="archived-heading" className="mt-4">
                <details className="group">
                  <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 [&::-webkit-details-marker]:hidden">
                    <h2 id="archived-heading" className="text-xs font-medium uppercase tracking-wide text-fg-subtle">
                      Archived
                    </h2>
                    <ChevronDown className="size-4 text-fg-subtle transition-transform group-open:rotate-180" aria-hidden />
                  </summary>
                  <ul className="space-y-0.5">
                    {archived.map((conversation) => (
                      <li key={conversation.id} className="flex min-w-0 items-center gap-1">
                        <NavLink
                          to={`/conversations/${conversation.id}`}
                          onClick={onNavigate}
                          className={({ isActive }) => `${row} ${isActive ? "bg-surface-2 text-fg" : ""}`}
                        >
                          <MessageSquare className="size-4 shrink-0" aria-hidden />
                          <span className="min-w-0">
                            <span className="block truncate">{conversation.title}</span>
                            <span className="block truncate text-xs text-fg-subtle">
                              {relativeTime(conversation.updated_at ?? conversation.created_at ?? "")}
                            </span>
                          </span>
                        </NavLink>
                        <button
                          type="button"
                          aria-label={`Unarchive ${conversation.title}`}
                          onClick={() => unarchiveConversation(conversation)}
                          className="inline-flex min-h-11 min-w-9 shrink-0 items-center justify-center rounded-xl text-fg-subtle transition-colors duration-150 hover:bg-surface-2 hover:text-fg"
                        >
                          <ArchiveRestore className="size-4" aria-hidden />
                        </button>
                      </li>
                    ))}
                  </ul>
                </details>
              </section>
            )}
          </>
        )}
      </div>

      <div className="space-y-1 border-t border-border p-2">
        {(actionError || deleteConversation.isError || deleteProject.isError || updateConversation.isError) && (
          <p role="alert" className="rounded-lg bg-danger-subtle px-2 py-1.5 text-xs text-danger">
            {actionError ?? deleteConversation.error?.message ?? deleteProject.error?.message ?? updateConversation.error?.message}
          </p>
        )}
        <div className="flex items-center gap-1">
          <NavLink
            to="/settings"
            onClick={onNavigate}
            className={({ isActive }) => `${row} ${isActive ? "bg-surface-2 text-fg" : ""}`}
          >
            <Settings className="size-5" aria-hidden /> Settings
          </NavLink>
          <ThemeToggle />
        </div>
      </div>

      <Dialog.Root open={workspaceOpen} onOpenChange={setWorkspaceOpen}>
        <Dialog.Portal>
          <Dialog.Backdrop className={dialogBackdropCls} />
          <Dialog.Popup className={dialogPopupCls}>
            <form
              className="p-5"
              onSubmit={(event) => {
                event.preventDefault();
                const name = workspaceName.trim();
                if (!name) return;
                const model = parseModelOptionValue(workspaceModel);
                const input = {
                  name,
                  instructions: workspaceInstructions.trim() || null,
                  endpoint_id: model?.provider_id ?? null,
                  model_id: model?.model_id ?? null,
                };
                const done = () => {
                  setWorkspaceName("");
                  setWorkspaceInstructions("");
                  setWorkspaceModel("");
                  setEditingWorkspace(null);
                  setWorkspaceOpen(false);
                };
                if (editingWorkspace) {
                  updateProject.mutate({ id: editingWorkspace.id, input }, { onSuccess: done });
                } else {
                  createProject.mutate(input, { onSuccess: done });
                }
              }}
            >
              <Dialog.Title className="text-lg font-semibold">{editingWorkspace ? "Edit workspace" : "New workspace"}</Dialog.Title>
              <Dialog.Description className="mt-1 text-sm text-fg-muted">
                Group related conversations and share instructions and model defaults.
              </Dialog.Description>
              <label className="mt-4 block text-sm font-medium text-fg-muted" htmlFor="workspace-name">Name</label>
              <input
                id="workspace-name"
                autoFocus
                value={workspaceName}
                onChange={(event) => setWorkspaceName(event.target.value)}
                className="mt-1 min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg"
              />
              <label className="mt-4 block text-sm font-medium text-fg-muted" htmlFor="workspace-instructions">Instructions</label>
              <textarea id="workspace-instructions" rows={3} value={workspaceInstructions} onChange={(event) => setWorkspaceInstructions(event.target.value)} placeholder="Shared instructions for conversations in this workspace" className="mt-1 w-full resize-y rounded-xl border border-border bg-surface-1 px-3 py-2 text-sm text-fg" />
              <label className="mt-4 block text-sm font-medium text-fg-muted" htmlFor="workspace-model">Default model</label>
              <select id="workspace-model" value={workspaceModel} onChange={(event) => setWorkspaceModel(event.target.value)} className="mt-1 min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg no-focus-ring">
                <option value="">No default</option>
                {enabledModels.data?.map((model) => <option key={modelOptionValue(model)} value={modelOptionValue(model)}>{model.provider_name} — {model.model_id}</option>)}
              </select>
              {(createProject.isError || updateProject.isError) && <p role="alert" className="mt-2 text-sm text-danger">{createProject.error?.message ?? updateProject.error?.message}</p>}
              <div className="mt-5 flex justify-end gap-2">
                <Dialog.Close className="min-h-11 rounded-xl px-4 text-sm text-fg-muted hover:bg-surface-2">Cancel</Dialog.Close>
                <button type="submit" disabled={!workspaceName.trim() || createProject.isPending || updateProject.isPending} className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50">{editingWorkspace ? "Save" : "Create"}</button>
              </div>
            </form>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>

      <Dialog.Root open={renaming !== null} onOpenChange={(open) => !open && setRenaming(null)}>
        <Dialog.Portal>
          <Dialog.Backdrop className={dialogBackdropCls} />
          <Dialog.Popup className={dialogPopupCls}>
            <form
              className="p-5"
              onSubmit={(event) => {
                event.preventDefault();
                const title = renameTitle.trim();
                if (!renaming || !title) return;
                updateConversation.mutate({ id: renaming.id, input: { title } }, { onSuccess: () => setRenaming(null) });
              }}
            >
              <Dialog.Title className="text-lg font-semibold">Rename conversation</Dialog.Title>
              <label htmlFor="conversation-title" className="mt-4 block text-sm font-medium text-fg-muted">Title</label>
              <input
                id="conversation-title"
                autoFocus
                value={renameTitle}
                onChange={(event) => setRenameTitle(event.target.value)}
                className="mt-1 min-h-11 w-full rounded-xl border border-border bg-surface-1 px-3 text-sm text-fg"
              />
              {updateConversation.isError && <p role="alert" className="mt-2 text-sm text-danger">{updateConversation.error.message}</p>}
              <div className="mt-5 flex justify-end gap-2">
                <Dialog.Close className="min-h-11 rounded-xl px-4 text-sm text-fg-muted hover:bg-surface-2">Cancel</Dialog.Close>
                <button type="submit" disabled={!renameTitle.trim() || updateConversation.isPending} className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50">Save</button>
              </div>
            </form>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}

function CompactRail() {
  const cycle = useRailModeStore((state) => state.cycle);
  return (
    <div className="flex h-full w-20 flex-col items-center gap-1 bg-surface-1 py-3">
      <button type="button" onClick={cycle} aria-label="Cycle sidebar display mode" className={iconButton}>
        <PanelLeft className="size-5" aria-hidden />
      </button>
      <NavLink to="/" aria-label="New conversation" className={iconButton}>
        <SquarePen className="size-5" aria-hidden />
      </NavLink>
      <NavLink to="/apps/knowledge-dump" aria-label="Apps" className={iconButton}>
        <Grid2X2 className="size-5" aria-hidden />
      </NavLink>
      <div className="flex-1" />
      <NavLink to="/settings" aria-label="Settings" className={iconButton}>
        <Settings className="size-5" aria-hidden />
      </NavLink>
      <ThemeToggle />
    </div>
  );
}

export default function AppShell() {
  const [sheetOpen, setSheetOpen] = useState(false);
  const mode = useRailModeStore((state) => state.mode);
  const cycle = useRailModeStore((state) => state.cycle);
  const browserFocus = useBrowserStore((state) => state.mode === "focus");

  return (
    <div className="flex h-dvh flex-col bg-surface-0 text-fg">
      <FileDropOverlay />
      <BrowserPermissionDialog />
      <WindowTitleBar />
      <div className="flex min-h-0 flex-1">
        <aside
          aria-label="Sidebar"
          aria-hidden={mode === "hidden"}
          className={`hidden shrink-0 overflow-hidden border-border transition-[width] duration-[var(--duration-base)] lg:block ${
            mode === "hidden" ? "w-0 border-r-0" : mode === "full" ? "w-72 border-r" : "w-20 border-r"
          }`}
        >
          {mode === "full" && <SidebarContent />}
          {mode === "compact" && <CompactRail />}
        </aside>

        {mode === "hidden" && (
          <button
            type="button"
            onClick={cycle}
            aria-label="Show sidebar"
            className="fixed left-3 top-12 z-30 hidden min-h-11 min-w-11 items-center justify-center rounded-full border border-border bg-surface-1 text-fg-muted shadow-md hover:text-fg lg:inline-flex"
          >
            <PanelLeft className="size-5" aria-hidden />
          </button>
        )}

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center gap-1 border-b border-border bg-surface-1 px-2 lg:hidden" style={{ paddingTop: "env(safe-area-inset-top)" }}>
            <button type="button" aria-label="Open menu" onClick={() => setSheetOpen(true)} className={iconButton}>
              <Menu className="size-5" aria-hidden />
            </button>
            <span className="ml-1 text-base font-semibold tracking-tight">Native GPT</span>
            <div className="flex-1" />
            <ThemeToggle />
          </header>
          {/* Browser panel lives in AppShell (spec §17) so it survives route
              changes. The center content shrinks first; the nav rail keeps its
              width. In focus mode the browser takes the whole content region. */}
          <div className="relative flex min-h-0 min-w-0 flex-1">
            <main
              className={`min-h-0 min-w-0 flex-1 overflow-hidden ${browserFocus ? "hidden" : ""}`}
            >
              <Outlet />
            </main>
            <BrowserPanel />
          </div>
        </div>

        <Dialog.Root open={sheetOpen} onOpenChange={setSheetOpen}>
          <Dialog.Portal>
            <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/40 transition-opacity data-[ending-style]:opacity-0 data-[starting-style]:opacity-0" />
            <Dialog.Popup aria-label="Menu" className="fixed inset-y-0 left-0 z-50 h-dvh w-72 max-w-[85vw] bg-surface-3 shadow-lg transition-transform data-[ending-style]:-translate-x-full data-[starting-style]:-translate-x-full">
              <Dialog.Title className="sr-only">Navigation</Dialog.Title>
              <Dialog.Close aria-label="Close menu" className="absolute right-2 top-2 z-10 inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl text-fg-muted hover:bg-surface-2 hover:text-fg">
                <X className="size-5" aria-hidden />
              </Dialog.Close>
              <SidebarContent onNavigate={() => setSheetOpen(false)} />
            </Dialog.Popup>
          </Dialog.Portal>
        </Dialog.Root>
      </div>
    </div>
  );
}
