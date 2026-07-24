"""Tests for tools/tool-router/tool.py."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "tool_router_under_test"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Router pointed at the REAL repo tools/ directory (DB still temp)."""
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.delenv("AGENTGPT_TOOLS_ROOT", raising=False)
    monkeypatch.delenv("AGENTGPT_MCP_SERVERS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


@pytest.fixture()
def fake_mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Router pointed at a fake tools root under tmp_path."""
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_TOOLS_ROOT", str(tmp_path / "tools"))
    (tmp_path / "tools").mkdir()
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "agentgpt.sqlite3"))
    conn.row_factory = sqlite3.Row
    return conn


def _make_tool(
    tmp_path: Path,
    tool_id: str,
    manifest: dict | None = None,
    with_tool_py: bool = True,
) -> None:
    folder = tmp_path / "tools" / tool_id
    folder.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_tool_py:
        (folder / "tool.py").write_text("TOOL = []\n", encoding="utf-8")


def _manifest(tool_id: str, **overrides) -> dict:
    base = {
        "id": tool_id,
        "name": tool_id.replace("-", " ").title(),
        "description": f"Tool for {tool_id} things",
        "version": "1.0.0",
        "trusted": True,
        "default_enabled": True,
        "risk": "read",
        "requires_approval": False,
    }
    base.update(overrides)
    return base


# ── search against the real tools directory ──────────────────────────────────


def test_search_finds_well_known_tool_ids(mod) -> None:
    result = mod.search_tools("knowledge search")
    ids = {h["tool_id"] for h in result["data"]["hits"]}
    assert "knowledge" in ids

    memory = mod.search_tools("scoped memory recall")
    assert "memory" in {h["tool_id"] for h in memory["data"]["hits"]}


def test_search_returns_only_enabled_tools(mod, tmp_path: Path) -> None:
    # Disable the knowledge tool via tool_settings; it must drop out of search.
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO tool_settings (tool_id, enabled, updated_at) VALUES ('knowledge', 0, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()
    conn.close()
    result = mod.search_tools("knowledge search")
    assert "knowledge" not in {h["tool_id"] for h in result["data"]["hits"]}
    # ...but details still report it, as disabled.
    details = mod.details("knowledge")
    assert details["data"]["enabled"] is False
    assert details["data"]["default_enabled"] is True


def test_search_honors_maximum_risk(mod) -> None:
    read_only = mod.search_tools("file", maximum_risk="read")
    assert read_only["ok"] is True
    assert all(h["risk"] == "read" for h in read_only["data"]["hits"])
    execute_allowed = mod.search_tools("shell execute command", maximum_risk="execute")
    assert execute_allowed["ok"] is True


def test_search_honors_required_capabilities(mod) -> None:
    hit = mod.search_tools("files", required_capabilities=["filesystem"])
    assert all(
        "filesystem" in (h["tool_id"] + h["name"] + (h["description"] or "")).lower()
        for h in hit["data"]["hits"]
    )


# ── search edge cases against a fake tools root ──────────────────────────────


def test_scan_excludes_folders_without_valid_manifests(fake_mod, tmp_path: Path) -> None:
    _make_tool(tmp_path, "good-tool", _manifest("good-tool", description="file searching"))
    _make_tool(tmp_path, "no-manifest", manifest=None)  # folder without manifest.json
    folder = tmp_path / "tools" / "broken-manifest"
    folder.mkdir()
    (folder / "manifest.json").write_text("{oops", encoding="utf-8")

    result = fake_mod.search_tools("file searching")
    assert [h["tool_id"] for h in result["data"]["hits"]] == ["good-tool"]
    listed = fake_mod.enabled_tools()
    assert [t["tool_id"] for t in listed["data"]["tools"]] == ["good-tool"]


def test_search_skips_default_disabled_tools(fake_mod, tmp_path: Path) -> None:
    _make_tool(
        tmp_path, "off-tool", _manifest("off-tool", default_enabled=False,
                                        description="file searching")
    )
    result = fake_mod.search_tools("file searching")
    assert result["data"]["count"] == 0
    details = fake_mod.details("off-tool")
    assert details["data"]["enabled"] is False


def test_search_empty_query_rejected(fake_mod) -> None:
    with pytest.raises(fake_mod.RouterToolError):
        fake_mod.search_tools("!!!")


# ── MCP metadata ─────────────────────────────────────────────────────────────


def test_include_mcp_surfaces_matching_servers(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "mcp_servers.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "gpu-bridge": {"url": "https://bridge.example/mcp",
                                   "transport": "streamable-http"},
                    "other": {"url": "https://other.example/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTGPT_MCP_SERVERS", str(config))
    result = mod.search_tools("bridge", include_mcp=True)
    servers = result["data"]["mcp_servers"]
    assert [s["server"] for s in servers] == ["gpu-bridge"]
    assert servers[0]["metadata_only"] is True
    assert servers[0]["url"] == "https://bridge.example/mcp"


def test_include_mcp_tolerates_missing_config(mod, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTGPT_MCP_SERVERS", str(tmp_path / "nope.json"))
    result = mod.search_tools("bridge", include_mcp=True)
    assert result["data"]["mcp_servers"] == []


# ── details / schema / enabled list ──────────────────────────────────────────


def test_get_details_real_tool(mod) -> None:
    result = mod.details("todo-list")
    assert result["ok"] is True
    assert result["data"]["has_tool_py"] is True
    assert result["data"]["manifest"]["id"] == "todo-list"


def test_get_details_unknown_tool(mod) -> None:
    with pytest.raises(mod.RouterToolError) as excinfo:
        mod.details("no-such-tool")
    assert excinfo.value.code == "not_found"


def test_get_tool_schema_returns_strands_specs(mod) -> None:
    result = mod.schema("calculate")
    assert result["ok"] is True, result
    assert result["data"]["count"] >= 1
    spec = result["data"]["specs"][0]
    assert spec["name"]
    assert "inputSchema" in spec
    assert spec["inputSchema"]["json"]["type"] == "object"


def test_get_tool_schema_reports_load_errors(fake_mod, tmp_path: Path) -> None:
    _make_tool(tmp_path, "bad-tool", _manifest("bad-tool"), with_tool_py=True)
    (tmp_path / "tools" / "bad-tool" / "tool.py").write_text(
        "raise RuntimeError('boom')\n", encoding="utf-8"
    )
    with pytest.raises(fake_mod.RouterToolError) as excinfo:
        fake_mod.schema("bad-tool")
    assert excinfo.value.code == "tool_load_error"


def test_list_enabled_tools_real_repo(mod) -> None:
    result = mod.enabled_tools()
    ids = {t["tool_id"] for t in result["data"]["tools"]}
    assert {"todo-list", "goal-supervisor", "memory", "knowledge"} <= ids
    scoped = mod.enabled_tools(scope="project", project_id="proj-1")
    assert scoped["data"]["context"]["project_id"] == "proj-1"


# ── grants ───────────────────────────────────────────────────────────────────


def test_grant_and_revoke_roundtrip(mod, tmp_path: Path) -> None:
    result = mod.grant("web-http", "project", scope_id="proj-1",
                       permissions={"allow": ["web_find"]})
    assert result["ok"] is True
    row = _db(tmp_path).execute(
        "SELECT * FROM tool_grants WHERE tool_id = 'web-http'"
    ).fetchone()
    assert row["scope"] == "project"
    assert row["scope_id"] == "proj-1"
    assert json.loads(row["permissions_json"]) == {"allow": ["web_find"]}

    revoked = mod.revoke("web-http", "project", scope_id="proj-1")
    assert revoked["data"]["revoked"] is True
    again = mod.revoke("web-http", "project", scope_id="proj-1")
    assert again["data"]["revoked"] is False


def test_grant_validates_scope_and_tool(mod) -> None:
    with pytest.raises(mod.RouterToolError) as excinfo:
        mod.grant("web-http", "galaxy")
    assert excinfo.value.code == "validation_error"
    with pytest.raises(mod.RouterToolError) as excinfo:
        mod.grant("no-such-tool", "user")
    assert excinfo.value.code == "not_found"


def test_grant_upserts_permissions(mod, tmp_path: Path) -> None:
    mod.grant("git-tools", "user", permissions={"push": False})
    mod.grant("git-tools", "user", permissions={"push": True})
    row = _db(tmp_path).execute(
        "SELECT permissions_json FROM tool_grants WHERE tool_id = 'git-tools'"
    ).fetchone()
    assert json.loads(row["permissions_json"]) == {"push": True}


# ── wrapper contract ─────────────────────────────────────────────────────────


def test_tool_wrapper_returns_error_dict(mod) -> None:
    result = mod.get_tool_details("no-such-tool")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_tool_export_lists_all_tools(mod) -> None:
    names = {t.tool_name for t in mod.TOOL}
    assert names == {
        "search_available_tools",
        "get_tool_details",
        "get_tool_schema",
        "list_enabled_tools",
        "grant_tool",
        "revoke_tool",
    }
