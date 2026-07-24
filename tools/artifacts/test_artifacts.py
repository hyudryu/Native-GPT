"""Tests for tools/artifacts/tool.py."""

from __future__ import annotations

import base64
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "artifacts_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_RUN_ID", raising=False)
    monkeypatch.delenv("AGENTGPT_CONVERSATION_ID", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "agentgpt.sqlite3"))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_project(tmp_path: Path, project_id: str, *conversation_ids: str) -> None:
    conn = _db(tmp_path)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, 'p', ?, ?)",
        (project_id, now, now),
    )
    for conversation_id in conversation_ids:
        conn.execute(
            "INSERT INTO conversations (id, project_id, title, created_at, updated_at)"
            " VALUES (?, ?, 'c', ?, ?)",
            (conversation_id, project_id, now, now),
        )
    conn.commit()
    conn.close()


def _create(mod, content: str = "hello world", **kwargs) -> dict:
    result = mod.create("note.md", "text/markdown", content=content, **kwargs)
    assert result["ok"] is True, result
    return result["data"]


# ── create ───────────────────────────────────────────────────────────────────


def test_create_from_text_persists_row_and_blob(mod, tmp_path: Path) -> None:
    data = _create(mod)
    assert data["size_bytes"] == len(b"hello world")
    assert len(data["sha256"]) == 64
    blob = tmp_path / data["storage_path"]
    assert blob.is_file()
    assert blob.read_text(encoding="utf-8") == "hello world"
    row = _db(tmp_path).execute(
        "SELECT * FROM artifacts WHERE id = ?", (data["artifact_id"],)
    ).fetchone()
    assert row is not None and row["deleted_at"] is None


def test_create_from_temp_path_copies_into_store(mod, tmp_path: Path) -> None:
    source = tmp_path / "in.txt"
    source.write_text("file body", encoding="utf-8")
    result = mod.create("in.txt", "text/plain", temp_path="in.txt")
    assert result["ok"] is True, result
    assert result["data"]["size_bytes"] == len(b"file body")
    assert source.is_file()  # original left in place (copy, not move)


def test_create_requires_exactly_one_source(mod) -> None:
    with pytest.raises(mod.ArtifactToolError) as excinfo:
        mod.create("x.txt", "text/plain")
    assert excinfo.value.code == "validation_error"
    with pytest.raises(mod.ArtifactToolError):
        mod.create("x.txt", "text/plain", content="a", temp_path="b")


def test_create_validates_mime_type(mod) -> None:
    with pytest.raises(mod.ArtifactToolError) as excinfo:
        mod.create("x.txt", "not-a-mime", content="a")
    assert excinfo.value.code == "validation_error"


def test_create_dedupes_identical_content(mod, tmp_path: Path) -> None:
    first = _create(mod, content="same bytes")
    second = _create(mod, content="same bytes")
    assert first["sha256"] == second["sha256"]
    assert first["storage_path"] == second["storage_path"]
    assert first["artifact_id"] != second["artifact_id"]


def test_create_defaults_conversation_from_context(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    monkeypatch.setenv("AGENTGPT_CONVERSATION_ID", "conv-1")
    data = _create(mod)
    assert data["conversation_id"] == "conv-1"
    assert data["project_id"] == "proj-1"  # resolved from the conversation


# ── get / list ───────────────────────────────────────────────────────────────


def test_get_returns_metadata(mod) -> None:
    created = _create(mod, created_by_tool="artifacts")
    result = mod.get(created["artifact_id"])
    assert result["ok"] is True
    assert result["data"]["name"] == "note.md"
    assert result["data"]["created_by_tool"] == "artifacts"


def test_get_unknown_id_raises(mod) -> None:
    with pytest.raises(mod.ArtifactToolError) as excinfo:
        mod.get("art_missing")
    assert excinfo.value.code == "not_found"


def test_list_filters_and_paginates(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    ids = [
        _create(mod, content=f"body {i}", conversation_id="conv-1")["artifact_id"]
        for i in range(3)
    ]
    _create(mod, content="other", conversation_id=None)

    page1 = mod.list_rows(conversation_id="conv-1", limit=2)
    assert page1["ok"] is True
    assert page1["data"]["count"] == 2
    assert page1["data"]["next_cursor"] is not None
    page2 = mod.list_rows(
        conversation_id="conv-1", limit=2, cursor=page1["data"]["next_cursor"]
    )
    assert page2["data"]["count"] == 1
    listed = {a["artifact_id"] for a in page1["data"]["artifacts"]} | {
        a["artifact_id"] for a in page2["data"]["artifacts"]
    }
    assert listed == set(ids)

    filtered = mod.list_rows(mime_type="text/markdown")
    assert filtered["data"]["count"] == 4
    none = mod.list_rows(mime_type="image/png")
    assert none["data"]["count"] == 0


# ── read / binary range ──────────────────────────────────────────────────────


def test_read_windows_text_with_truncation_flags(mod) -> None:
    created = _create(mod, content="0123456789")
    result = mod.read(created["artifact_id"], offset=2, length=4)
    assert result["ok"] is True
    assert result["data"]["content"] == "2345"
    assert result["data"]["total_chars"] == 10
    assert result["data"]["truncated_before"] is True
    assert result["data"]["truncated_after"] is True
    tail = mod.read(created["artifact_id"], offset=6)
    assert tail["data"]["truncated_after"] is False


def test_read_refuses_binary_and_points_to_range_reader(mod, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    created = mod.create("blob.bin", "application/octet-stream", temp_path="blob.bin")
    assert created["ok"] is True, created
    artifact_id = created["data"]["artifact_id"]

    with pytest.raises(mod.ArtifactToolError) as excinfo:
        mod.read(artifact_id)
    assert excinfo.value.code == "binary_content"
    assert "read_artifact_binary_range" in str(excinfo.value)


def test_read_binary_range_returns_base64(mod, tmp_path: Path) -> None:
    payload = b"\xff\xfe\x00\x01" + bytes(range(64))
    (tmp_path / "blob.bin").write_bytes(payload)
    created = mod.create("blob.bin", "application/octet-stream", temp_path="blob.bin")
    artifact_id = created["data"]["artifact_id"]

    result = mod.read_binary_range(artifact_id, offset=4, length=16)
    assert result["ok"] is True
    assert base64.b64decode(result["data"]["content_base64"]) == payload[4:20]
    assert result["data"]["size_bytes"] == len(payload)
    assert result["data"]["truncated_after"] is True

    with pytest.raises(mod.ArtifactToolError):
        mod.read_binary_range(artifact_id, offset=0, length=2 * 1024 * 1024)


# ── rename / delete / preview / metadata / copy ─────────────────────────────


def test_rename_updates_name(mod) -> None:
    created = _create(mod)
    result = mod.rename(created["artifact_id"], "renamed.md")
    assert result["ok"] is True
    assert result["data"]["previous_name"] == "note.md"
    assert mod.get(created["artifact_id"])["data"]["name"] == "renamed.md"


def test_soft_delete_excludes_from_lists_but_keeps_blob(mod, tmp_path: Path) -> None:
    created = _create(mod)
    result = mod.soft_delete(created["artifact_id"])
    assert result["ok"] is True and result["data"]["blob_retained"] is True
    assert (tmp_path / created["storage_path"]).is_file()  # blob kept
    assert mod.list_rows()["data"]["count"] == 0
    with pytest.raises(mod.ArtifactToolError):
        mod.get(created["artifact_id"])
    meta = mod.metadata(created["artifact_id"])
    assert meta["data"]["deleted_at"] is not None  # metadata still auditable


def test_preview_text_and_binary(mod, tmp_path: Path) -> None:
    text = _create(mod, content="x" * 5000)
    result = mod.preview(text["artifact_id"])
    assert result["data"]["binary"] is False
    assert result["data"]["truncated"] is True
    assert result["data"]["preview"].startswith("x" * 100)

    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    binary = mod.create("img.png", "image/png", temp_path="img.png")
    result = mod.preview(binary["data"]["artifact_id"])
    assert result["data"]["binary"] is True
    assert result["data"]["preview"] is None


def test_metadata_includes_storage_info(mod, tmp_path: Path) -> None:
    created = _create(mod)
    result = mod.metadata(created["artifact_id"])
    assert result["ok"] is True
    storage = result["data"]["storage"]
    assert storage["blob_exists"] is True
    assert storage["absolute_path"].endswith(created["sha256"])


def test_copy_to_project_writes_under_root(mod, tmp_path: Path) -> None:
    created = _create(mod, content="deliverable")
    result = mod.copy_to_project(created["artifact_id"], "out/final.md")
    assert result["ok"] is True
    assert (tmp_path / "out" / "final.md").read_text(encoding="utf-8") == "deliverable"


def test_copy_to_project_rejects_escape(mod) -> None:
    created = _create(mod)
    with pytest.raises(mod.ArtifactToolError) as excinfo:
        mod.copy_to_project(created["artifact_id"], "../../outside.md")
    assert excinfo.value.code == "invalid_path"


# ── wrapper error contract ───────────────────────────────────────────────────


def test_tool_wrapper_returns_error_dict(mod) -> None:
    result = mod.get_artifact("art_missing")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"
    assert result["data"] == {}


def test_tool_export_lists_all_tools(mod) -> None:
    names = {t.tool_name for t in mod.TOOL}
    assert names == {
        "create_artifact",
        "get_artifact",
        "list_artifacts",
        "read_artifact",
        "read_artifact_binary_range",
        "rename_artifact",
        "delete_artifact",
        "get_artifact_preview",
        "get_artifact_metadata",
        "copy_artifact_to_project",
    }
