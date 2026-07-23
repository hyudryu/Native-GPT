"""Dynamic Tool Router Strands tools — discover, inspect, and grant tools.

Multi-tool folder: `TOOL` is a list of Strands tools. The router scans
`tools/*/manifest.json` under the tools root (`$AGENTGPT_TOOLS_ROOT` when set
— the test hook — else `<repo>/tools/`), mirroring the host's discovery
(`crates/server/src/tools.rs`): folders without a valid manifest are ignored.

Enablement: `tool_settings` (migration 0003) holds host-managed enable/disable
rows; a tool with no row falls back to its manifest's `default_enabled`.
search_available_tools returns ENABLED tools only; get_tool_details and
list_enabled_tools expose the state itself.

Grants: grant_tool/revoke_tool manage `tool_grants` rows (migration 0011)
keyed (tool_id, scope, scope_id). ENFORCEMENT of grants happens at the host
layer (planned) — these tools only manage the grant records.

Trust model for get_tool_schema: loading a tool.py EXECUTES its module code
(importlib, same spec_from_file_location approach as the runtime registry in
apps/agent-runtime/tools/registry.py). This is acceptable because every tool
folder in this repository is a trusted built-in vetted by the host — the same
code the runtime itself would import when the tool is enabled. The router
never loads folders outside the tools root and validates the tool id before
resolving any path.

MCP: include_mcp reads the MCP server registry (`$AGENTGPT_MCP_SERVERS` when
set, else `<repo>/app-data/mcp_servers.json`; see
apps/agent-runtime/mcp_servers.py for the shape — an {"mcpServers": {...}}
mapping or a flat name→config mapping). Listing a remote server's actual tool
names requires a live MCP handshake, which a discovery tool must not do, so
MCP results are METADATA-ONLY (server name, url, transport).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands import tool

# Load shared `_lib` helpers by file path (no package context when the
# runtime imports this file standalone).
_LIB_DIR = Path(__file__).resolve().parent.parent / "_lib"


def _load_lib(filename: str, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, _LIB_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_db = _load_lib("db.py", "agentgpt_tools_db")
_paths = _load_lib("paths.py", "agentgpt_tools_paths")

TOOL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RISK_ORDER = {"read": 0, "write": 1, "execute": 2}
GRANT_SCOPES = ("user", "profile", "project", "conversation")
SEARCH_LIMIT_MAX = 50
LIST_LIMIT_MAX = 200


class RouterToolError(ValueError):
    """Any tool-router failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ok(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouterToolError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise RouterToolError("db_unavailable", str(exc)) from exc


def _tools_root() -> Path:
    override = os.environ.get("AGENTGPT_TOOLS_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return _paths.repo_root() / "tools"


def _mcp_config_path() -> Path:
    override = os.environ.get("AGENTGPT_MCP_SERVERS", "").strip()
    if override:
        return Path(override).resolve()
    return _paths.repo_root() / "app-data" / "mcp_servers.json"


def _validate_tool_id(tool_id: Any) -> str:
    tool_id = _require_text(tool_id, "tool_id")
    if not TOOL_ID_RE.fullmatch(tool_id):
        raise RouterToolError("validation_error", f"invalid tool id: {tool_id!r}")
    return tool_id


def _tool_dir(tool_id: str) -> Path:
    tool_id = _validate_tool_id(tool_id)
    directory = (_tools_root() / tool_id).resolve()
    if _tools_root() not in directory.parents:
        raise RouterToolError("validation_error", f"invalid tool id: {tool_id!r}")
    return directory


def _read_manifest(directory: Path) -> dict[str, Any] | None:
    """Parse a tool folder's manifest; None when missing/invalid (host skips it)."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return None
    return data


def _scan() -> list[dict[str, Any]]:
    """All tool folders with a valid manifest, sorted by id."""
    root = _tools_root()
    found: list[dict[str, Any]] = []
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest = _read_manifest(child)
        if manifest is None:
            continue  # mirrors host discovery: invalid/absent manifest = not a tool
        found.append({"manifest": manifest, "directory": child})
    found.sort(key=lambda entry: entry["manifest"]["id"])
    return found


def _risk(manifest: dict[str, Any]) -> str:
    risk = manifest.get("risk")
    # Unknown/absent risk is treated as "execute" — fail-safe for filtering.
    return risk if risk in RISK_ORDER else "execute"


def _is_enabled(conn: sqlite3.Connection, tool_id: str, manifest: dict[str, Any]) -> bool:
    row = conn.execute(
        "SELECT enabled FROM tool_settings WHERE tool_id = ?", (tool_id,)
    ).fetchone()
    if row is not None:
        return bool(row["enabled"])
    return bool(manifest.get("default_enabled", False))


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2}


def _summary(
    conn: sqlite3.Connection, entry: dict[str, Any], score: float | None = None
) -> dict[str, Any]:
    manifest = entry["manifest"]
    tool_id = manifest["id"]
    data: dict[str, Any] = {
        "tool_id": tool_id,
        "name": manifest.get("name", tool_id),
        "description": manifest.get("description"),
        "version": manifest.get("version"),
        "risk": _risk(manifest),
        "trusted": bool(manifest.get("trusted", False)),
        "requires_approval": bool(manifest.get("requires_approval", False)),
        "enabled": _is_enabled(conn, tool_id, manifest),
        "default_enabled": bool(manifest.get("default_enabled", False)),
        "has_tool_py": (entry["directory"] / "tool.py").is_file(),
        "path": str(entry["directory"]),
    }
    if score is not None:
        data["score"] = score
    return data


def _load_mcp_servers() -> dict[str, Any]:
    """mcpServers mapping from the registry file; {} when missing/malformed."""
    path = _mcp_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers", data)
    return servers if isinstance(servers, dict) else {}


# ── plain implementations ───────────────────────────────────────────────────


def search_tools(
    query: str,
    required_capabilities: Any = None,
    maximum_risk: str | None = None,
    include_mcp: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Token-match search over enabled tools; see search_available_tools."""
    query = _require_text(query, "query")
    if required_capabilities is None:
        capabilities: list[str] = []
    elif isinstance(required_capabilities, str):
        capabilities = [required_capabilities]
    elif isinstance(required_capabilities, list) and all(
        isinstance(c, str) for c in required_capabilities
    ):
        capabilities = required_capabilities
    else:
        raise RouterToolError(
            "validation_error", "required_capabilities must be a list of strings"
        )
    if maximum_risk is not None and maximum_risk not in RISK_ORDER:
        raise RouterToolError(
            "validation_error", f"maximum_risk must be one of {tuple(RISK_ORDER)}"
        )
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise RouterToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > SEARCH_LIMIT_MAX:
        raise RouterToolError(
            "validation_error", f"limit must be between 1 and {SEARCH_LIMIT_MAX}"
        )

    query_terms = _tokens(query)
    if not query_terms:
        raise RouterToolError("validation_error", "query has no searchable terms")

    conn = _connect()
    try:
        hits: list[dict[str, Any]] = []
        for entry in _scan():
            manifest = entry["manifest"]
            tool_id = manifest["id"]
            if not _is_enabled(conn, tool_id, manifest):
                continue
            if maximum_risk is not None and RISK_ORDER[_risk(manifest)] > RISK_ORDER[
                maximum_risk
            ]:
                continue
            haystack_text = " ".join(
                [tool_id, str(manifest.get("name", "")), str(manifest.get("description", ""))]
            ).lower()
            if any(capability.lower() not in haystack_text for capability in capabilities):
                continue
            id_terms = _tokens(tool_id.replace("-", " "))
            name_terms = _tokens(str(manifest.get("name", "")))
            description_terms = _tokens(str(manifest.get("description", "")))
            total = len(query_terms)
            score = (
                0.2 * len(query_terms & id_terms) / total
                + 0.4 * len(query_terms & name_terms) / total
                + 0.4 * len(query_terms & description_terms) / total
            )
            if score <= 0:
                continue
            hits.append(_summary(conn, entry, score=round(score, 4)))
        hits.sort(key=lambda hit: (-hit["score"], hit["tool_id"]))
        hits = hits[:limit]

        mcp_hits: list[dict[str, Any]] = []
        if include_mcp:
            for name, config in _load_mcp_servers().items():
                name_tokens = _tokens(str(name).replace("-", " ").replace("_", " "))
                if not query_terms & name_tokens:
                    continue
                config = config if isinstance(config, dict) else {}
                mcp_hits.append(
                    {
                        "server": name,
                        "url": config.get("url"),
                        "transport": config.get("transport", "streamable-http"),
                        "metadata_only": True,
                        "note": "remote tool names require a live MCP handshake; "
                        "only server metadata is searchable",
                    }
                )
            mcp_hits.sort(key=lambda hit: hit["server"])

        return _ok(
            f"{len(hits)} tool(s) matching {query!r}",
            {
                "hits": hits,
                "count": len(hits),
                "query": query,
                "mcp_servers": mcp_hits,
            },
        )
    finally:
        conn.close()


def details(tool_id: str) -> dict[str, Any]:
    """Manifest + folder presence + enabled state for one tool."""
    directory = _tool_dir(tool_id)
    if not directory.is_dir():
        raise RouterToolError("not_found", f"tool not found: {tool_id}")
    manifest = _read_manifest(directory)
    if manifest is None:
        raise RouterToolError("not_found", f"tool {tool_id} has no valid manifest")
    conn = _connect()
    try:
        data = _summary(conn, {"manifest": manifest, "directory": directory})
        data["manifest"] = manifest
        return _ok(f"tool {tool_id}: {data['name']}", data)
    finally:
        conn.close()


def schema(tool_id: str) -> dict[str, Any]:
    """Strands tool spec(s) for a tool folder; loading executes module code."""
    directory = _tool_dir(tool_id)
    module_path = directory / "tool.py"
    if not module_path.is_file():
        raise RouterToolError("not_found", f"tool {tool_id} has no tool.py")
    module_name = f"agentgpt_router_probe_{tool_id.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RouterToolError("tool_load_error", f"could not build a spec for {tool_id}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RouterToolError(
            "tool_load_error", f"tool {tool_id} failed to load: {exc}"
        ) from exc
    finally:
        sys.modules.pop(module_name, None)
    exported = getattr(module, "TOOL", None)
    if exported is None:
        raise RouterToolError("tool_load_error", f"tool {tool_id} exports no TOOL")
    tools = list(exported) if isinstance(exported, (list, tuple)) else [exported]
    specs: list[dict[str, Any]] = []
    for item in tools:
        tool_spec = getattr(item, "tool_spec", None)
        if isinstance(tool_spec, dict):
            specs.append(tool_spec)
        else:
            specs.append({"name": getattr(item, "__name__", str(item)), "raw": True})
    return _ok(
        f"tool {tool_id}: {len(specs)} Strands tool spec(s)",
        {"tool_id": tool_id, "specs": specs, "count": len(specs)},
    )


def enabled_tools(
    scope: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """List enabled tools (tool_settings join + manifest defaults)."""
    conn = _connect()
    try:
        tools = [
            _summary(conn, entry) for entry in _scan() if _is_enabled(
                conn, entry["manifest"]["id"], entry["manifest"]
            )
        ]
        return _ok(
            f"{len(tools)} enabled tool(s)",
            {
                "tools": tools,
                "count": len(tools),
                # tool_settings is global-only today; scope ids are accepted for
                # forward compatibility and echoed for the caller's context.
                "context": {
                    "scope": scope,
                    "project_id": project_id,
                    "conversation_id": conversation_id,
                    "profile_id": profile_id,
                },
            },
        )
    finally:
        conn.close()


def grant(
    tool_id: str,
    scope: str,
    scope_id: str | None = None,
    permissions: Any = None,
) -> dict[str, Any]:
    """Upsert a tool_grants row; enforcement is a host-layer concern (planned)."""
    tool_id = _validate_tool_id(tool_id)
    if scope not in GRANT_SCOPES:
        raise RouterToolError(
            "validation_error", f"scope must be one of {GRANT_SCOPES}"
        )
    scope_id = (scope_id or "").strip()
    if permissions is not None:
        try:
            permissions_json = json.dumps(permissions, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise RouterToolError(
                "validation_error", f"permissions must be JSON-serializable: {exc}"
            ) from exc
    else:
        permissions_json = None
    directory = _tool_dir(tool_id)
    if not directory.is_dir() or _read_manifest(directory) is None:
        raise RouterToolError("not_found", f"tool not found: {tool_id}")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO tool_grants (tool_id, scope, scope_id, permissions_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(tool_id, scope, scope_id) DO UPDATE SET"
            " permissions_json = excluded.permissions_json",
            (tool_id, scope, scope_id, permissions_json, _now()),
        )
        conn.commit()
        return _ok(
            f"grant recorded: {tool_id} at {scope} scope",
            {
                "tool_id": tool_id,
                "scope": scope,
                "scope_id": scope_id,
                "permissions": permissions,
                "enforcement": "host-layer (planned); this manages the grant record only",
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revoke(tool_id: str, scope: str, scope_id: str | None = None) -> dict[str, Any]:
    """Delete a tool_grants row; reports whether one existed."""
    tool_id = _validate_tool_id(tool_id)
    if scope not in GRANT_SCOPES:
        raise RouterToolError(
            "validation_error", f"scope must be one of {GRANT_SCOPES}"
        )
    scope_id = (scope_id or "").strip()
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM tool_grants WHERE tool_id = ? AND scope = ? AND scope_id = ?",
            (tool_id, scope, scope_id),
        )
        conn.commit()
        return _ok(
            f"grant revoked: {tool_id} at {scope} scope"
            if cursor.rowcount
            else f"no grant existed for {tool_id} at {scope} scope",
            {
                "tool_id": tool_id,
                "scope": scope,
                "scope_id": scope_id,
                "revoked": cursor.rowcount > 0,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Strands tool wrappers ─────────────────────────────────────────────────


def _wrap(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except RouterToolError as exc:
        return {
            "ok": False,
            "summary": str(exc),
            "data": {},
            "error": {"code": exc.code, "message": str(exc)},
        }
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "summary": f"database error: {exc}",
            "data": {},
            "error": {"code": "db_error", "message": str(exc)},
        }


@tool
def search_available_tools(
    query: str,
    required_capabilities: list[str] | None = None,
    maximum_risk: str | None = None,
    include_mcp: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Search ENABLED tools by capability (id/name/description token match).

    Use this to find the right tool before attempting a task. Risk filtering
    is inclusive (read < write < execute): maximum_risk="read" returns only
    read-risk tools. Disabled tools and folders without valid manifests are
    excluded. include_mcp additionally surfaces matching MCP servers from the
    registry file — metadata only (remote tool names need a live handshake).

    Args:
        query: Free-text capability search (e.g. "read files", "git commit").
        required_capabilities: Tokens that must each appear in the tool's
            id, name, or description.
        maximum_risk: read | write | execute — exclude riskier tools.
        include_mcp: Also search MCP server metadata (default False).
        limit: Maximum hits (1-50, default 10).

    Returns:
        `{ok, summary, data: {hits: [{tool_id, name, description, risk,
        enabled, score, ...}], count, mcp_servers: [...]}, error}`.
    """
    return _wrap(search_tools, query, required_capabilities, maximum_risk, include_mcp, limit)


@tool
def get_tool_details(tool_id: str) -> dict[str, Any]:
    """Manifest, folder presence, and enabled state for one tool.

    Args:
        tool_id: Tool folder id (e.g. "todo-list").

    Returns:
        `{ok, summary, data: {tool_id, name, description, risk, enabled,
        has_tool_py, manifest, ...}, error}`.
    """
    return _wrap(details, tool_id)


@tool
def get_tool_schema(tool_id: str) -> dict[str, Any]:
    """Load a tool and return its Strands tool spec(s) (name, docs, parameters).

    NOTE: loading executes the tool module's top-level code (same import
    mechanism as the runtime registry); safe because repository tools are
    trusted built-ins. Nothing is invoked — only the exported specs are read.

    Args:
        tool_id: Tool folder id (e.g. "knowledge").

    Returns:
        `{ok, summary, data: {tool_id, specs: [{name, description,
        inputSchema}], count}, error}`.
    """
    return _wrap(schema, tool_id)


@tool
def list_enabled_tools(
    scope: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """List all currently enabled tools (tool_settings join + manifest defaults).

    tool_settings is global today; the scope/project/conversation/profile
    arguments are accepted for forward compatibility and echoed back in the
    response context.

    Returns:
        `{ok, summary, data: {tools: [...], count, context}, error}`.
    """
    return _wrap(enabled_tools, scope, project_id, conversation_id, profile_id)


@tool
def grant_tool(
    tool_id: str,
    scope: str,
    scope_id: str | None = None,
    permissions: Any = None,
) -> dict[str, Any]:
    """Record a tool grant (upserts a tool_grants row).

    Grants are RECORDS for the host layer, which will enforce them at runtime
    (planned); this tool manages the records only.

    Args:
        tool_id: Tool to grant.
        scope: user | profile | project | conversation.
        scope_id: Id within the scope (empty for scope-wide grants).
        permissions: Optional JSON-serializable permission detail.

    Returns:
        `{ok, summary, data: {tool_id, scope, scope_id, permissions,
        enforcement}, error}`.
    """
    return _wrap(grant, tool_id, scope, scope_id, permissions)


@tool
def revoke_tool(tool_id: str, scope: str, scope_id: str | None = None) -> dict[str, Any]:
    """Revoke a tool grant (deletes the tool_grants row).

    Args:
        tool_id: Tool whose grant to revoke.
        scope: user | profile | project | conversation.
        scope_id: Id within the scope (empty for scope-wide grants).

    Returns:
        `{ok, summary, data: {tool_id, scope, scope_id, revoked}, error}`.
    """
    return _wrap(revoke, tool_id, scope, scope_id)


TOOL = [
    search_available_tools,
    get_tool_details,
    get_tool_schema,
    list_enabled_tools,
    grant_tool,
    revoke_tool,
]
