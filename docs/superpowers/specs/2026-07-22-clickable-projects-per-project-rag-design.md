# Clickable Projects with Per-Project RAG & Conversations

**Date:** 2026-07-22
**Status:** Approved (brainstorming complete)
**Reference:** Screenshot showing a project detail view with "Chats" and "Sources" tabs, scoped to the selected project.

## Goal

Make projects (currently labeled "Workspaces") clickable so that clicking one opens a project detail view with two tabs — **Chats** and **Sources** (RAG) — scoped to that project. RAG becomes per-project instead of global.

## Current State

- **Projects** exist in the API and UI sidebar (`AppShell.tsx:349-400`), labeled "Workspaces".
- **Clicking a project does nothing today** — the project header row (`AppShell.tsx:369-394`) is a non-interactive `<div>` with a Folder icon, name, conversation count, a "+" button, and a `WorkspaceMenu`. There is no `projects/:id` route in `App.tsx`.
- **Conversations are already per-project** at the API level: `GET /api/conversations?project_id=ID` is supported (`phase3.rs:246-253`, `ConversationQuery` at `phase3.rs:47-52`), and the sidebar already filters client-side (`AppShell.tsx:366`).
- **RAG ("Knowledge") is global.** `knowledge_sources` (`db.rs:184-195`) has **no `project_id`**. All sources are injected into every chat's system prompt via `context_for_prompt` (`knowledge.rs:277-301`), called from `chat.rs:85` with no project scoping.

## Decisions (from brainstorming)

1. **Per-project RAG** via a nullable `project_id` on `knowledge_sources`. `NULL` = global (preserves existing behavior for ungrouped chats); non-null = scoped to that project.
2. **Layout:** Single route `/projects/:projectId` with internal tab state (Chats | Sources). Project detail fills the main content area next to the existing sidebar.
3. **Sidebar:** Keep the current nested layout (conversations under each project) AND make the project name clickable. The "+" and `WorkspaceMenu` are unchanged.

## Design

### 1. Data Model — migration `0005_project_knowledge.sql`

```sql
-- 0005_project_knowledge: scope knowledge sources to a project.
-- NULL = global source (available to all chats, unchanged behavior).
-- Non-NULL = scoped to that project only.
ALTER TABLE knowledge_sources ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE CASCADE;
CREATE INDEX idx_knowledge_sources_project_id ON knowledge_sources(project_id);
```

Cascade on delete mirrors how deleting a project already cascades to its scoped knowledge. Existing rows get `NULL` (global) automatically.

### 2. Backend — `crates/server/src/db.rs`

- **`KnowledgeSourceRow`** (lines 184-195): add `pub project_id: Option<String>`.
- **`KNOWLEDGE_SOURCE_COLUMNS`** (line 371-372): add `project_id` as the last column.
- **`knowledge_source_from_row`** (lines 344-355): read `project_id: row.get("project_id")?`.
- **`insert_knowledge_source`** (lines 1120-1164): add `project_id` to the INSERT columns and params (9th bind value).
- **`list_knowledge_sources(project_id: Option<&str>)`** (line 1166): scope the query — `Some(id)` → `WHERE project_id = ?` (that project's sources only); `None` → `WHERE project_id IS NULL` (global sources only). The existing `KnowledgeDumpPage` / `BrainPage` call with `None` continues to show global sources.
- **`list_knowledge_chunks(project_id: Option<&str>)`** (line 1180): join `knowledge_sources` and filter so the chunk enumeration used by `search_db` only considers chunks belonging to the project's own sources plus global sources. Concretely: `WHERE s.project_id IS NULL OR s.project_id = ?`.
- **`delete_project`** (existing): no change needed — `ON DELETE CASCADE` handles cleanup now.

### 3. Backend — `crates/server/src/knowledge.rs`

Thread `project_id` through the read/ingest/search paths:

- **`IngestKnowledge`** (lines 23-31): add `#[serde(default)] project_id: Option<String>`. When set, `validate_project_id` (reuse from `phase3.rs:106`) should confirm it exists before inserting.
- **`search_db(state, query, limit, project_id: Option<&str>)`** (lines 247-275): pass `project_id` into `list_knowledge_chunks` so candidate chunks are project-scoped + global.
- **`context_for_prompt(state, prompt, project_id: Option<&str>)`** (lines 277-301): pass `project_id` to `search_db`. The prompt preamble wording stays as-is.
- **`list_sources`** (lines 303-310): accept `Query(KnowledgeListQuery { project_id: Option<String> })` and call `list_knowledge_sources(project_id.as_deref())`. When omitted → global; when set → that project's sources. (Global dump page continues to pass nothing → global.)
- **`ingest`** (lines 312-372): set `source.project_id = body.project_id` on the built `KnowledgeSourceRow`.
- **`search`** (lines 386-398): accept optional `project_id` query param, forward to `search_db`.

### 4. Backend — `crates/server/src/chat.rs`

At lines 76-91, the knowledge context call (line 85) currently ignores project:

```rust
let knowledge_context = crate::knowledge::context_for_prompt(&state, content).await?;
```

Change to pass the conversation's project id so project chats pull project + global sources:

```rust
let knowledge_context =
    crate::knowledge::context_for_prompt(&state, content, conversation.project_id.as_deref()).await?;
```

Ungrouped chats (`project_id = None`) get global sources only — unchanged behavior.

### 5. Backend — router (`crates/server/src/lib.rs`)

No new routes. The existing `/api/knowledge` GET/POST and `/api/knowledge/search` GET gain optional query/body params. The existing `/api/conversations?project_id=` is reused as-is.

### 6. Frontend — route + page

**`apps/ui/src/App.tsx`:** add a child route `projects/:projectId` → `ProjectPage`.

**New file `apps/ui/src/pages/ProjectPage.tsx`:** mirrors the `AppPage` wrapper pattern (`features/apps/AppPage.tsx`) and `KnowledgeDumpPage` patterns.
- Reads `projectId` from `useParams`.
- Loads the project via a new `useProject(id)` hook; shows a not-found state if missing.
- Header: project name, edit affordance (opens the existing workspace dialog), model badge if a default model is set.
- Tab switcher with two tabs, tracked via `useState<"chats" | "sources">` initialized from a `?tab=` query param (defaults to `"chats"`).
- **Chats tab:** list of the project's conversations via `GET /api/conversations?project_id=ID` (new `useProjectConversations(projectId)` hook, or extend the existing `useConversations` query). Each row navigates to `/conversations/:id`. Includes a "New conversation" button that creates one scoped to the project and navigates to it.
- **Sources tab:** the project's RAG source list via `GET /api/knowledge?project_id=ID` (extend `useKnowledge(projectId)`), with the same ingest options as `KnowledgeDumpPage` (paste / file / URL) scoped to the project, plus delete. Empty state copy explains that sources added here only apply to this project.

### 7. Frontend — make projects clickable (`apps/ui/src/layout/AppShell.tsx`)

At lines 369-394, wrap the project name (currently a `<span>`) in a `NavLink to={\`/projects/${project.id}\`}` that navigates to the detail view. Keep:
- The Folder icon, conversation count, and active-state styling (NavLink `isActive`).
- The "+" button (new conversation in project) — unchanged.
- The `WorkspaceMenu` (edit/delete) — unchanged.
- The nested conversation list under the project row (`items.map(conversationRow)` at line 395) — unchanged.

The `onNavigate` callback is forwarded so mobile sheet closes on navigation.

### 8. Frontend — data hooks

**`apps/ui/src/lib/dataApi.ts`:**
- `useProject(id)` — `GET /api/projects/:id` (new hook; `get_project` endpoint already exists at `phase3.rs:191`).
- `useProjectConversations(projectId)` — `GET /api/conversations?project_id=ID&archived=false`. Could be implemented by extending `useConversations` to accept an optional `projectId` argument that becomes a query param; or as a separate hook. Preferred: extend the existing query key to include `projectId` so cache scoping is correct.

**`apps/ui/src/lib/appsApi.ts`:**
- `KnowledgeSource` type: add `project_id?: string | null`.
- `useKnowledge(projectId?)` — pass `project_id` as a query param when provided; keep `undefined` (global) for existing callers (`KnowledgeDumpPage`, `BrainPage`).
- `useIngestKnowledge` — accept and forward `project_id` in the POST body.
- `useKnowledgeSearch(projectId?)` — forward `project_id` to the search endpoint.

### 9. Edge cases

- **Delete project:** conversations become ungrouped (`ON DELETE SET NULL`, existing). Project-scoped knowledge sources are removed (`ON DELETE CASCADE`, new migration). Global sources untouched.
- **Project not found:** `ProjectPage` shows a "Project not found" empty state (reuse `NotFoundPage` styling/pattern) with a link back to home.
- **Global vs project scope in chat:** a project's chats use project sources **plus** global sources (`WHERE project_id IS NULL OR project_id = ?`). Ungrouped chats use global sources only. No existing chat behavior changes.
- **Mobile:** the bottom-nav "Workspaces" button (`AppShell.tsx:635`) opens the slide-out sheet as today; tapping a project in the sheet navigates to `/projects/:id` and closes the sheet via `onNavigate`.
- **Existing apps:** `KnowledgeDumpPage` and `BrainPage` keep working against global sources — they pass `projectId = undefined`, which lists/searches `project_id IS NULL` rows. (Decision: global knowledge pages show only global sources, not project-scoped ones. This keeps "app-wide Knowledge" meaning global.)

## Out of Scope (YAGNI)

- Separate management UI for global vs. per-project sources within a project view (a project's Sources tab shows only that project's sources; global sources are managed in the existing Knowledge pages).
- Per-project analytics.
- Drag-and-drop reordering of conversations between projects.
- Bulk move conversations between projects.
- Sharing a single source across multiple specific projects (a source is either global or bound to exactly one project).

## Testing

- **Rust unit tests** (`db.rs` / `knowledge.rs`): extend existing knowledge tests to cover (a) ingesting with a `project_id`, (b) listing sources filtered by project, (c) searching chunks returns project + global matches, (d) deleting a project cascades to its knowledge sources.
- **Rust integration tests** (`phase3.rs` test module): add a test that creates a project, ingests a project-scoped source, and asserts `GET /api/knowledge?project_id=` returns only that source; verify an ungrouped chat still receives only global context.
- **Frontend:** type-check (`npm run typecheck`) and lint (`npm run lint`) pass. Manual smoke test of the new route + tab interactions (click project → detail view, Chats tab lists project conversations, Sources tab lists/adds/deletes project sources, chat uses project + global RAG).

## Files Touched

**Backend (Rust):**
- `crates/server/migrations/0005_project_knowledge.sql` (new)
- `crates/server/src/db.rs` (row struct, columns constant, mapper, insert/list methods)
- `crates/server/src/knowledge.rs` (request structs, search_db, context_for_prompt, handlers)
- `crates/server/src/chat.rs` (pass project_id to context_for_prompt)

**Frontend (React/TS):**
- `apps/ui/src/App.tsx` (new route)
- `apps/ui/src/pages/ProjectPage.tsx` (new)
- `apps/ui/src/layout/AppShell.tsx` (clickable project names)
- `apps/ui/src/lib/dataApi.ts` (useProject, project-scoped conversations hook)
- `apps/ui/src/lib/appsApi.ts` (project_id on KnowledgeSource + hooks)
