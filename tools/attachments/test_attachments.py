"""Tests for tools/attachments/tool.py."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "attachments_tool_under_test"
_ARTIFACTS_STORE_NAME = "agentgpt_tools_artifacts_store"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    create_test_db(tmp_path)
    monkeypatch.setenv("AGENTGPT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_RUN_ID", raising=False)
    monkeypatch.delenv("AGENTGPT_CONVERSATION_ID", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME, _ARTIFACTS_STORE_NAME)


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


def _write_source(tmp_path: Path, name: str, content: str) -> str:
    (tmp_path / name).write_text(content, encoding="utf-8")
    return name


# ── attach ───────────────────────────────────────────────────────────────────


def test_attach_from_source_path_imports_and_links(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "notes.txt", "meeting notes")
    result = mod.attach(conversation_id="conv-1", source_path="notes.txt")
    assert result["ok"] is True, result
    data = result["data"]
    assert data["name"] == "notes.txt"
    assert data["project_id"] == "proj-1"
    row = _db(tmp_path).execute(
        "SELECT * FROM attachments WHERE id = ?", (data["attachment_id"],)
    ).fetchone()
    assert row is not None and row["artifact_id"] == data["artifact_id"]
    artifact = _db(tmp_path).execute(
        "SELECT * FROM artifacts WHERE id = ?", (data["artifact_id"],)
    ).fetchone()
    assert artifact is not None and artifact["created_by_tool"] == "attachments"


def test_attach_existing_artifact_links_without_reimport(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    artifact = mod._artifacts.create("doc.md", "text/markdown", content="# hi")
    artifact_id = artifact["data"]["artifact_id"]
    before = _db(tmp_path).execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
    result = mod.attach(conversation_id="conv-1", artifact_id=artifact_id)
    assert result["ok"] is True, result
    assert result["data"]["artifact_id"] == artifact_id
    after = _db(tmp_path).execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
    assert before == after


def test_attach_requires_exactly_one_source(mod) -> None:
    with pytest.raises(mod.AttachmentToolError) as excinfo:
        mod.attach()
    assert excinfo.value.code == "validation_error"
    with pytest.raises(mod.AttachmentToolError):
        mod.attach(source_path="a.txt", artifact_id="art_x")


def test_attach_missing_file_reports_not_found(mod) -> None:
    with pytest.raises(mod.AttachmentToolError) as excinfo:
        mod.attach(source_path="nope.txt")
    assert excinfo.value.code == "not_found"


def test_attach_defaults_conversation_from_context(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    monkeypatch.setenv("AGENTGPT_CONVERSATION_ID", "conv-1")
    _write_source(tmp_path, "a.txt", "alpha")
    result = mod.attach(source_path="a.txt")
    assert result["data"]["conversation_id"] == "conv-1"


# ── list / metadata ──────────────────────────────────────────────────────────


def test_list_filters_by_conversation_and_paginates(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1", "conv-2")
    for i in range(3):
        _write_source(tmp_path, f"f{i}.txt", f"body {i}")
        mod.attach(conversation_id="conv-1", source_path=f"f{i}.txt")
    _write_source(tmp_path, "other.txt", "other")
    mod.attach(conversation_id="conv-2", source_path="other.txt")

    page1 = mod.list_rows(conversation_id="conv-1", limit=2)
    assert page1["data"]["count"] == 2
    page2 = mod.list_rows(
        conversation_id="conv-1", limit=2, cursor=page1["data"]["next_cursor"]
    )
    assert page2["data"]["count"] == 1
    names = {a["name"] for a in page1["data"]["attachments"] + page2["data"]["attachments"]}
    assert names == {"f0.txt", "f1.txt", "f2.txt"}


def test_metadata_joins_artifact(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "meta.txt", "metadata target")
    attached = mod.attach(conversation_id="conv-1", source_path="meta.txt")["data"]
    result = mod.metadata(attached["attachment_id"])
    assert result["ok"] is True
    assert result["data"]["artifact"]["artifact_id"] == attached["artifact_id"]
    assert result["data"]["artifact"]["size_bytes"] == len(b"metadata target")


# ── read ─────────────────────────────────────────────────────────────────────


def test_read_character_window(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "win.txt", "0123456789")
    attached = mod.attach(conversation_id="conv-1", source_path="win.txt")["data"]
    result = mod.read(attached["attachment_id"], offset=3, length=4)
    assert result["ok"] is True
    assert result["data"]["mode"] == "window"
    assert result["data"]["content"] == "3456"
    assert result["data"]["truncated_before"] is True
    assert result["data"]["truncated_after"] is True


def test_read_page_windows_are_200_lines(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    lines = [f"line {i}" for i in range(1, 451)]  # 450 lines -> 3 pages
    _write_source(tmp_path, "paged.txt", "\n".join(lines))
    attached = mod.attach(conversation_id="conv-1", source_path="paged.txt")["data"]

    page2 = mod.read(attached["attachment_id"], page=2)
    assert page2["data"]["mode"] == "page"
    assert page2["data"]["total_pages"] == 3
    assert page2["data"]["content"].startswith("line 201")
    assert page2["data"]["content"].endswith("line 400")
    assert page2["data"]["truncated_before"] is True
    assert page2["data"]["truncated_after"] is True

    chunk = mod.open_attachment_chunk(attached["attachment_id"], page=3)
    assert chunk["ok"] is True
    assert chunk["data"]["content"].startswith("line 401")
    assert chunk["data"]["truncated_after"] is False

    with pytest.raises(mod.AttachmentToolError):
        mod.read(attached["attachment_id"], page=0)


def test_read_refuses_unsupported_binary_format(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    attached = mod.attach(conversation_id="conv-1", source_path="doc.pdf")["data"]
    assert attached["mime_type"] == "application/pdf"
    with pytest.raises(mod.AttachmentToolError) as excinfo:
        mod.read(attached["attachment_id"])
    assert excinfo.value.code == "unsupported_content"
    # But it is still listed and inspectable.
    assert mod.list_rows(conversation_id="conv-1")["data"]["count"] == 1
    assert mod.metadata(attached["attachment_id"])["ok"] is True


# ── search ───────────────────────────────────────────────────────────────────


def test_search_finds_keyword_with_context(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "alpha.txt", "the quick brown fox jumps over")
    _write_source(tmp_path, "beta.txt", "nothing relevant here")
    mod.attach(conversation_id="conv-1", source_path="alpha.txt")
    mod.attach(conversation_id="conv-1", source_path="beta.txt")

    result = mod.search("BROWN fox", conversation_id="conv-1")
    assert result["ok"] is True
    assert result["data"]["count"] == 1
    hit = result["data"]["hits"][0]
    assert hit["name"] == "alpha.txt"
    assert "quick brown fox jumps" in hit["context"]
    assert result["data"]["attachments_scanned"] == 2


def test_search_honors_file_types_and_skips_unsupported(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "data.csv", "id,name\n1,widget\n")
    _write_source(tmp_path, "notes.txt", "widget notes")
    (tmp_path / "sheet.xlsx").write_bytes(b"PK\x03\x04 fake")
    mod.attach(conversation_id="conv-1", source_path="data.csv")
    mod.attach(conversation_id="conv-1", source_path="notes.txt")
    mod.attach(conversation_id="conv-1", source_path="sheet.xlsx")

    csv_only = mod.search("widget", conversation_id="conv-1", file_types=["csv"])
    assert {h["name"] for h in csv_only["data"]["hits"]} == {"data.csv"}

    everything = mod.search("widget", conversation_id="conv-1")
    assert {h["name"] for h in everything["data"]["hits"]} == {"data.csv", "notes.txt"}
    skipped = everything["data"]["attachments_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["name"] == "sheet.xlsx"
    assert skipped[0]["reason"] == "unsupported_content"

    with pytest.raises(mod.AttachmentToolError):
        mod.search("widget", search_mode="semantic")


def test_search_scopes_by_conversation(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1", "conv-2")
    _write_source(tmp_path, "secret.txt", "needle in conv-2")
    mod.attach(conversation_id="conv-2", source_path="secret.txt")
    result = mod.search("needle", conversation_id="conv-1")
    assert result["data"]["count"] == 0
    result = mod.search("needle", conversation_id="conv-2")
    assert result["data"]["count"] == 1


# ── remove / rename ──────────────────────────────────────────────────────────


def test_remove_soft_deletes_link_but_keeps_artifact(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "gone.txt", "still stored")
    attached = mod.attach(conversation_id="conv-1", source_path="gone.txt")["data"]
    result = mod.remove(attached["attachment_id"])
    assert result["ok"] is True and result["data"]["artifact_kept"] is True
    assert mod.list_rows(conversation_id="conv-1")["data"]["count"] == 0
    artifact = _db(tmp_path).execute(
        "SELECT * FROM artifacts WHERE id = ?", (attached["artifact_id"],)
    ).fetchone()
    assert artifact["deleted_at"] is None
    with pytest.raises(mod.AttachmentToolError):
        mod.read(attached["attachment_id"])


def test_rename_changes_display_name(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    _write_source(tmp_path, "old.txt", "rename me")
    attached = mod.attach(conversation_id="conv-1", source_path="old.txt")["data"]
    result = mod.rename(attached["attachment_id"], "new-name.txt")
    assert result["ok"] is True
    assert result["data"]["previous_name"] == "old.txt"
    assert mod.metadata(attached["attachment_id"])["data"]["name"] == "new-name.txt"


# ── wrapper contract ─────────────────────────────────────────────────────────


def test_tool_wrapper_returns_error_dict(mod) -> None:
    result = mod.read_attachment("att_missing")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_tool_export_lists_all_tools(mod) -> None:
    names = {t.tool_name for t in mod.TOOL}
    assert names == {
        "attach_file",
        "list_attachments",
        "get_attachment_metadata",
        "read_attachment",
        "search_attachments",
        "open_attachment_chunk",
        "remove_attachment",
        "rename_attachment",
    }
