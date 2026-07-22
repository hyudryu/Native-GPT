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
    paths.py
    test_paths.py
```

`manifest.json` describes the tool in the Native GPT Tools screen. `tool.py`
exports a `TOOL` value accepted by the Strands `Agent(tools=[...])` API. Keeping
tool-owned assets in the same folder prevents runtime downloads from cluttering
the repository.

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

