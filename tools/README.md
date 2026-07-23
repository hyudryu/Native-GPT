# Native GPT tools

Every Strands tool lives in its own folder under this directory:

```text
tools/
  tool-slug/
    manifest.json
    tool.py
    test_tool.py        # optional pytest, run via `uv run pytest` from apps/agent-runtime
    ...tool-owned downloads and data...
  _lib/                 # shared helpers — NOT a tool (no manifest/tool.py)
    paths.py            # path safety / allowed roots
    db.py               # shared SQLite access (host DB conventions)
    context.py          # current run_id / conversation_id
    testdb.py           # test helper: build a scratch DB from the real migrations
    vectorize.py        # deterministic feature-hash embeddings (memory/knowledge)
    secrets_scan.py     # credential-shaped content detector (memory/knowledge)
    web_safety.py       # shared SSRF / unsafe-URL guard (web-fetch, web-http)
    test_paths.py
```

`manifest.json` describes the tool in the Native GPT Tools screen. `tool.py`
exports a `TOOL` value accepted by the Strands `Agent(tools=[...])` API —
either a single tool or a list of tools for multi-tool folders (e.g.
`todo-list`). Keeping tool-owned assets in the same folder prevents runtime
downloads from cluttering the repository.

## Discovery

The Rust host (`crates/server/src/tools.rs`) scans `tools/` for folders that
contain **both** `manifest.json` and `tool.py`. Folders missing either file
(like `_lib/`) are ignored — that's how shared helpers stay out of the tool
registry.

The Python runtime (`apps/agent-runtime/.../tools/registry.py`) imports each
tool's `tool.py` as a standalone module via `spec_from_file_location`. There's
no package context, so cross-folder imports need to load the file directly by
path — see `tools/read-file/tool.py` for the pattern used to load `_lib/paths.py`.

## Result schema

Tools that produce structured results (file/web operations, anything that may
emit more than a short string) return the standard result dict:

```python
{
    "ok": bool,
    "summary": "One-line human-readable description",
    "data": {...},        # structured payload
    "error": None | {"code": str, "message": str},
}
```

On the wire, the runtime wraps these as `run.tool_result` envelopes (see
`packages/protocol-types/schemas/messages.json`). The UI's tool-call renderer
(`apps/ui/src/pages/ChatPage.tsx`) shows `summary` inline and `data` / `error`
in a collapsible disclosure.

## Path safety

Filesystem tools (`read-file`, `list-files`, `search-files`) refuse to follow
paths outside the allowed roots. The default root is the repo
(`AGENTGPT_REPO_ROOT`); extend it with `AGENTGPT_ALLOWED_ROOTS` (OS path
separator-delimited) to allow reading from e.g. an indexed documents folder.
Traversal (`../../etc/passwd`) and absolute paths outside the roots are
rejected before any disk access. See `_lib/paths.py`.

## Database-backed tools

Tools like `todo-list` (planner / micro-goals), `goal-supervisor` (goal
contracts + deterministic validation), `memory` (scoped assistant memory
with hybrid FTS + feature-vector recall), and `knowledge` (domain knowledge
RAG over the host's Knowledge Dump tables) persist to the app's SQLite database
via `_lib/db.py`, which mirrors the host's path resolution
(`AGENTGPT_DATA_DIR`, else `<repo>/app-data/database/agentgpt.sqlite3`) and
pragmas (WAL, busy timeout, foreign keys). Their tables come from migration
`0011_agent_intelligence` (plus the pre-existing `knowledge_sources` /
`knowledge_chunks` from 0003/0005 for the knowledge tool). The `artifacts`,
`attachments`, `notifications`, `skills`, and `tool-router` tools also live
on 0011 tables (`artifacts`, `attachments`, `notifications`, `skills` +
`skill_settings`, `tool_grants`) plus `tool_settings` from 0003. Tools stay
stdlib-only (sqlite3, json, uuid, datetime, hashlib) — no pip dependencies.

The memory and knowledge tools share the credential guard in
`_lib/secrets_scan.py`: content that looks like an API key, password, token,
or private key is rejected with `sensitive_content_rejected` (no override).

Other multi-tool folders:

- `utilities` (risk: read) — deterministic unit conversion, timezone and
  datetime parsing (IANA zones via zoneinfo), date differences, UUIDs, and
  text/file hashing.
- `fs-extensions` (risk: write, approval-gated) — stat with sha256,
  hash-verified copy, a reversible trash store, binary range reads, and
  zip-slip-guarded zip/tar.gz archives. The trash store is a JSON manifest +
  blobs under the app data dir (`$AGENTGPT_DATA_DIR/trash/` else
  `<repo>/app-data/trash/`) because migration 0011 has no `trash_records`
  table and new migrations are out of scope — see the tool's module docstring.
- `git-tools` (risk: write, no approval gate — documented in its manifest;
  mutations are local/reversible and push is limited to --force-with-lease) —
  structured status/diff/log/show, branching, staging/commit, fetch/pull/push,
  merge/rebase/abort, and conflict resolution via the git CLI.
- `web-http` (risk: read, outbound network) — `web_find` (search a fetched
  URL or a knowledge source) and a general `http_request` verb client. Both
  share the SSRF guard with `web-fetch` via `_lib/web_safety.py` (every
  redirect hop re-validated; credentials in URLs refused).
- `dev-tools` (risk: execute, approval-gated) — auto-detected test/lint/
  format runners plus `inspect_build_errors` (structured rustc/pytest/tsc
  diagnostics). Same sanitized-env execution pattern as `shell-execute`.
- `artifacts` (risk: write) — durable artifact store: content-addressed blobs
  under `$AGENTGPT_DATA_DIR/artifacts/` (else `<repo>/app-data/artifacts/`),
  metadata in the host's `artifacts` table (0011). Windowed text reads, 1 MB
  capped base64 binary ranges, previews, keyset listing, soft delete (blobs
  retained). The goal-supervisor's `artifact_exists` validator reads these
  rows. render/download variants are planned (need UI/renderers).
- `attachments` (risk: write) — conversation attachments built on the
  artifact store: attach files or existing artifacts, read text attachments
  by 200-line page or character window, bounded keyword search with match
  context, rename/detach. PDF/DOCX/XLSX/images are stored and listed but
  content access returns a clear unsupported error (parser stage planned).
- `notifications` (risk: read) — persistent user notifications in the host's
  `notifications` table (0011): send with urgency, list unread/all
  (dismissed excluded), mark read, dismiss. Delivery is via the table — the
  host UI surfaces rows; the tools never push.
- `skills` (risk: read) — instructional skills registry over `<repo>/skills/`
  folders (`skill.json` manifest + `SKILL.md` prompt; `$AGENTGPT_SKILLS_ROOT`
  overrides the root): list/get/search, scoped enable/disable via
  `skill_settings`, validation, install/uninstall (built-in publisher
  "Native GPT" skills are uninstall-protected), dependency resolution.
  Built-ins: `critical-thinking`, `plan-execute-verify`.
- `tool-router` (risk: read) — dynamic tool discovery: token-match search
  over `tools/*/manifest.json` with risk (read<write<execute) and enabled
  filters (`tool_settings` join + manifest defaults), manifest details,
  on-demand Strands schema loading (trusted built-ins; same import mechanism
  as the runtime registry), enabled-tool listing, and `tool_grants` record
  management (enforcement is host-layer, planned). MCP search is
  metadata-only via `mcp_servers.json`.


The knowledge tool stores per-source metadata (tags, `content_sha256` for
dedupe, and `embedding_version` for vector provenance) inside
`knowledge_sources.tags_json` as a JSON object, because no new migrations may
add columns. Python-embedded sources are scored with vector cosine;
host-ingested (Rust-vector) sources rank lexically until `knowledge_reindex`
rebuilds their embeddings.

The memory tool blends lexical FTS5 matching with cosine similarity over
deterministic feature-hash vectors from `_lib/vectorize.py` (a Python port of
the Rust host's `vectorize` in `crates/server/src/knowledge.rs`; vectors are
not bit-compatible with Rust-produced ones, so stored embeddings carry an
`embedding_version` and can be rebuilt).

Scoped tools learn the active run/conversation from `_lib/context.py`: the
runtime's `run_context` context vars when called inside a run, the
`AGENTGPT_RUN_ID` / `AGENTGPT_CONVERSATION_ID` env vars otherwise. With no
context (e.g. unit tests), tools work unscoped or take explicit ids.

## Running tests

```bash
cd apps/agent-runtime
uv run pytest                      # tests/ + tools/**/test_*.py
uv run pytest ../../tools/calculate/test_tool.py
```

Tool tests live next to the tool (`test_tool.py`) and use
`importlib.util.spec_from_file_location` to load `tool.py` the same way the
runtime's loader does, so they exercise the actual tool code without needing
Strands to be wired up.

