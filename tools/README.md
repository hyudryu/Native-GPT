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
contracts + deterministic validation), and `memory` (scoped assistant memory
with hybrid FTS + feature-vector recall) persist to the app's SQLite database
via `_lib/db.py`, which mirrors the host's path resolution
(`AGENTGPT_DATA_DIR`, else `<repo>/app-data/database/agentgpt.sqlite3`) and
pragmas (WAL, busy timeout, foreign keys). Their tables come from migration
`0011_agent_intelligence`. Tools stay stdlib-only (sqlite3, json, uuid,
datetime, hashlib) — no pip dependencies.

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

