"""Tests for tools/knowledge/tool.py."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "knowledge_tool_under_test"


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


def _ingest(mod, title: str, content: str, **kwargs) -> str:
    result = mod.knowledge_ingest(title=title, content=content, **kwargs)
    assert result["ok"] is True, result
    assert result["data"]["duplicate_of"] is None
    return result["data"]["source_id"]


def _hit_source_ids(result) -> list[str]:
    assert result["ok"] is True, result
    return [hit["source_id"] for hit in result["data"]["hits"]]


RUST_TEXT = (
    "Rust ownership enforces memory safety without a garbage collector. "
    "The borrow checker tracks lifetimes and prevents data races at compile time."
)
FINANCE_TEXT = (
    "Dollar cost averaging spreads investments over time to reduce volatility risk. "
    "Index funds tracking broad markets keep fees low for long horizon savers."
)


# ── ingest + search round trip ────────────────────────────────────────────────


def test_ingest_and_search_round_trip(mod) -> None:
    source_id = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    result = mod.knowledge_search("memory safety borrow checker")
    hits = result["data"]["hits"]
    assert _hit_source_ids(result) == [source_id]
    hit = hits[0]
    assert hit["ref"] == "K1"
    assert hit["title"] == "Rust notes"
    assert "borrow checker" in hit["snippet"]
    assert hit["score"] > 0
    assert result["data"]["citations"]["K1"]["source_id"] == source_id

    fetched = mod.knowledge_get_source(source_id)
    assert fetched["ok"] is True
    assert fetched["data"]["chunk_count"] == 1
    assert fetched["data"]["embedding_version"] == "feature-hash-v1-py"
    assert fetched["data"]["content_sha256"]

    chunk = mod.knowledge_get_chunk(source_id, hit["chunk_id"])
    assert chunk["ok"] is True
    assert chunk["data"]["position"] == 0
    assert chunk["data"]["previous"] is None
    assert chunk["data"]["next"] is None


def test_get_chunk_neighbors(mod) -> None:
    long_text = " ".join(f"word{i}" for i in range(400))  # 3 chunks at 180/30
    source_id = _ingest(mod, "long doc", long_text, scope="global")
    result = mod.knowledge_search("word200 word201")
    hit = result["data"]["hits"][0]
    chunk = mod.knowledge_get_chunk(source_id, hit["chunk_id"])
    assert chunk["ok"] is True
    assert chunk["data"]["chunk_count"] == 3
    if chunk["data"]["position"] == 1:
        assert chunk["data"]["previous"]["position"] == 0
        assert chunk["data"]["next"]["position"] == 2


# ── scope visibility ──────────────────────────────────────────────────────────


def test_global_visible_from_unrelated_conversation(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a", "conv-b")
    source_id = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    hits = _hit_source_ids(mod.knowledge_search("borrow checker", conversation_id="conv-b"))
    assert source_id in hits


def test_project_and_global_visibility(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    _seed_project(tmp_path, "proj-2", "conv-c")
    global_id = _ingest(mod, "Global rust", RUST_TEXT, scope="global")
    project_id_ = _ingest(
        mod, "Project finance", FINANCE_TEXT, scope="project", project_id="proj-1"
    )
    # Inside proj-1's conversation: both visible.
    hits = _hit_source_ids(
        mod.knowledge_search("memory safety index funds", conversation_id="conv-a")
    )
    assert global_id in hits and project_id_ in hits
    # From another project's conversation: only the global source is visible.
    result = mod.knowledge_search("index funds volatility", conversation_id="conv-c")
    assert project_id_ not in _hit_source_ids(result)
    assert global_id in _hit_source_ids(
        mod.knowledge_search("borrow checker", conversation_id="conv-c")
    )


def test_conversation_isolation(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a", "conv-b")
    source_id = _ingest(
        mod, "Conv-a notes", RUST_TEXT, scope="conversation", conversation_id="conv-a"
    )
    assert source_id in _hit_source_ids(
        mod.knowledge_search("borrow checker", conversation_id="conv-a")
    )
    assert source_id not in _hit_source_ids(
        mod.knowledge_search("borrow checker", conversation_id="conv-b")
    )
    # And invisible to a context-free query.
    assert source_id not in _hit_source_ids(mod.knowledge_search("borrow checker"))


def test_scope_filter_restricts_results(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    global_id = _ingest(mod, "Global rust", RUST_TEXT, scope="global")
    project_source = _ingest(
        mod, "Project rust", RUST_TEXT + " Extra project detail.", scope="project",
        project_id="proj-1",
    )
    only_project = _hit_source_ids(
        mod.knowledge_search("borrow checker", scopes=["project"], conversation_id="conv-a")
    )
    assert project_source in only_project
    assert global_id not in only_project
    only_global = _hit_source_ids(
        mod.knowledge_search("borrow checker", scopes=["global"], conversation_id="conv-a")
    )
    assert global_id in only_global
    assert project_source not in only_global
    # A scope with no resolvable context id hides entirely.
    assert mod.knowledge_search("borrow checker", scopes=["conversation"])["data"]["hits"] == []


def test_host_created_project_source_treated_as_project_scoped(mod, tmp_path: Path) -> None:
    """Rows written by the Rust host (scope column left 'global', project_id set)
    must still be treated as project-scoped (effective scope)."""
    _seed_project(tmp_path, "proj-1", "conv-a")
    _seed_project(tmp_path, "proj-2", "conv-c")
    conn = _db(tmp_path)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO knowledge_sources (id, title, source_type, content, chunk_count,"
        " created_at, updated_at, project_id) VALUES ('host-src', 'host doc', 'paste', ?, 1, ?, ?, 'proj-1')",
        (RUST_TEXT, now, now),
    )
    conn.execute(
        "INSERT INTO knowledge_chunks (id, source_id, position, content, embedding_json, created_at)"
        " VALUES ('host-chk', 'host-src', 0, ?, '[]', ?)",
        (RUST_TEXT, now),
    )
    conn.commit()
    conn.close()
    assert "host-src" in _hit_source_ids(
        mod.knowledge_search("borrow checker", conversation_id="conv-a")
    )
    assert "host-src" not in _hit_source_ids(
        mod.knowledge_search("borrow checker", conversation_id="conv-c")
    )
    # Vector scoring is skipped for non-Python embeddings, but lexical still ranks it.
    hit = [
        h
        for h in mod.knowledge_search("borrow checker", conversation_id="conv-a")["data"]["hits"]
        if h["source_id"] == "host-src"
    ][0]
    assert hit["score_components"]["vector"] == 0.0
    assert hit["score_components"]["lexical"] > 0.0


# ── citations audit trail ─────────────────────────────────────────────────────


def test_citations_recorded_with_run_context(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    monkeypatch.setenv("AGENTGPT_RUN_ID", "run-1")
    monkeypatch.setenv("AGENTGPT_CONVERSATION_ID", "conv-a")
    source_id = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    result = mod.knowledge_search("borrow checker")
    hit = result["data"]["hits"][0]
    conn = _db(tmp_path)
    rows = conn.execute(
        "SELECT * FROM knowledge_citations WHERE source_id = ?", (source_id,)
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["chunk_id"] == hit["chunk_id"]
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["conversation_id"] == "conv-a"


def test_citations_skipped_without_context(mod, tmp_path: Path) -> None:
    _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    mod.knowledge_search("borrow checker")
    conn = _db(tmp_path)
    count = conn.execute("SELECT COUNT(*) FROM knowledge_citations").fetchone()[0]
    conn.close()
    assert count == 0


# ── promotion ─────────────────────────────────────────────────────────────────


def test_promotion_ladder_and_global_warning(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    source_id = _ingest(
        mod, "Conv notes", RUST_TEXT, scope="conversation", conversation_id="conv-a"
    )
    promoted = mod.knowledge_promote(source_id, "project")
    assert promoted["ok"] is True
    assert promoted["data"]["scope"] == "project"
    assert promoted["data"]["project_id"] == "proj-1"
    assert promoted["data"]["warning"] is None
    # Now visible to the whole project, not just conv-a.
    _seed_project(tmp_path, "proj-1b", "conv-z")
    promoted2 = mod.knowledge_promote(source_id, "global")
    assert promoted2["ok"] is True
    assert promoted2["data"]["warning"]
    got = mod.knowledge_get_source(source_id)
    assert got["data"]["scope"] == "global"
    assert got["data"]["project_id"] is None
    # Demotion rejected.
    demoted = mod.knowledge_promote(source_id, "project", project_id="proj-1")
    assert demoted["ok"] is False
    assert demoted["error"]["code"] == "invalid_promotion"


# ── dedupe / secrets ──────────────────────────────────────────────────────────


def test_dedupe_by_content_hash(mod) -> None:
    first = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    again = mod.knowledge_ingest(title="Rust notes copy", content=RUST_TEXT, scope="global")
    assert again["ok"] is True
    assert again["data"]["duplicate_of"] == first
    assert again["data"]["created"] is False
    listed = mod.knowledge_list_sources(scope="global")
    assert listed["data"]["count"] == 1
    # Same content at a different scope is NOT a duplicate.
    other = mod.knowledge_ingest(
        title="Rust notes", content=RUST_TEXT, scope="project", project_id=None,
        conversation_id=None,
    )
    assert other["ok"] is False  # project scope needs a resolvable project
    _seeded = mod.knowledge_ingest(title="Rust notes", content=RUST_TEXT + " ", scope="global")
    assert _seeded["data"]["duplicate_of"] == first  # canonicalization stable


def test_secret_rejection(mod) -> None:
    result = mod.knowledge_ingest(
        title="leak", content="config: api_key = 'abcdef123456789'", scope="global"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "sensitive_content_rejected"
    listed = mod.knowledge_list_sources(scope="global")
    assert listed["data"]["count"] == 0


def test_url_ingest_rejected_with_web_fetch_guidance(mod) -> None:
    result = mod.knowledge_ingest(
        title="page", urls=["https://example.com/spec"], scope="global"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "url_fetch_unsupported"
    assert "web_fetch" in result["error"]["message"]


def test_artifact_content_ingest(mod, tmp_path: Path) -> None:
    artifact_file = tmp_path / "notes.txt"
    artifact_file.write_text(FINANCE_TEXT, encoding="utf-8")
    conn = _db(tmp_path)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO artifacts (id, name, mime_type, storage_path, created_at, updated_at)"
        " VALUES ('art-1', 'notes.txt', 'text/plain', ?, ?, ?)",
        (str(artifact_file), now, now),
    )
    conn.commit()
    conn.close()
    result = mod.knowledge_ingest(
        title="Finance artifact", artifact_ids=["art-1"], scope="global"
    )
    assert result["ok"] is True, result
    assert result["data"]["source_id"] in _hit_source_ids(
        mod.knowledge_search("dollar cost averaging")
    )
    got = mod.knowledge_get_source(result["data"]["source_id"])
    assert got["data"]["source_type"] == "file"


# ── update / reindex / delete ─────────────────────────────────────────────────


def test_update_rechunks(mod) -> None:
    source_id = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    long_text = " ".join(f"token{i}" for i in range(400))
    updated = mod.knowledge_update(source_id, content=long_text, tags=["ref"])
    assert updated["ok"] is True
    assert updated["data"]["content_changed"] is True
    assert updated["data"]["chunk_count"] == 3
    got = mod.knowledge_get_source(source_id)
    assert got["data"]["chunk_count"] == 3
    assert got["data"]["tags"] == ["ref"]
    # Old content no longer matches; new content does.
    assert source_id not in _hit_source_ids(mod.knowledge_search("borrow checker"))
    assert source_id in _hit_source_ids(mod.knowledge_search("token250 token251"))


def test_reindex_heals_embedding_version(mod, tmp_path: Path) -> None:
    source_id = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    # Simulate a host-ingested (Rust-vector) source: strip version metadata.
    conn = _db(tmp_path)
    row = conn.execute(
        "SELECT tags_json FROM knowledge_sources WHERE id = ?", (source_id,)
    ).fetchone()
    meta = mod._unpack_meta(row["tags_json"])
    meta["embedding_version"] = "feature-hash-v1-rust"
    import json as _json_mod

    conn.execute(
        "UPDATE knowledge_sources SET tags_json = ? WHERE id = ?",
        (_json_mod.dumps(meta), source_id),
    )
    conn.commit()
    conn.close()
    before = mod.knowledge_search("borrow checker")
    hit = [h for h in before["data"]["hits"] if h["source_id"] == source_id][0]
    assert hit["score_components"]["vector"] == 0.0
    reindexed = mod.knowledge_reindex(source_id)
    assert reindexed["ok"] is True
    assert reindexed["data"]["previous_embedding_version"] == "feature-hash-v1-rust"
    assert reindexed["data"]["embedding_version"] == "feature-hash-v1-py"
    after = mod.knowledge_search("borrow checker")
    hit = [h for h in after["data"]["hits"] if h["source_id"] == source_id][0]
    assert hit["score_components"]["vector"] > 0.0


def test_soft_delete_excludes_from_search_and_list(mod) -> None:
    source_id = _ingest(mod, "Rust notes", RUST_TEXT, scope="global")
    deleted = mod.knowledge_delete(source_id)
    assert deleted["ok"] is True
    assert deleted["data"]["deleted"] is True
    assert source_id not in _hit_source_ids(mod.knowledge_search("borrow checker"))
    assert mod.knowledge_list_sources(scope="global")["data"]["count"] == 0
    again = mod.knowledge_delete(source_id)
    assert again["data"]["already_deleted"] is True
    # Deleting twice is fine; updating a deleted source is not.
    assert mod.knowledge_update(source_id, title="x")["error"]["code"] == "not_found"


# ── merge ─────────────────────────────────────────────────────────────────────


def test_merge_sources(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    a = _ingest(mod, "Rust notes", RUST_TEXT, scope="global", tags=["lang"])
    b = _ingest(mod, "Finance notes", FINANCE_TEXT, scope="global", tags=["money"])
    merged = mod.knowledge_merge([a, b], "Combined reference", "project", project_id="proj-1")
    assert merged["ok"] is True, merged
    new_id = merged["data"]["source_id"]
    got = mod.knowledge_get_source(new_id)
    assert got["data"]["scope"] == "project"
    assert got["data"]["tags"] == ["lang", "money"]
    assert "# Source: Rust notes" in got["data"]["content_preview"]
    assert "# Source: Rust notes" in mod.knowledge_get_chunk(
        new_id,
        mod.knowledge_search("borrow checker", conversation_id="conv-a")["data"]["hits"][0]["chunk_id"],
    )["data"]["content"]
    # Originals are soft-deleted and gone from search.
    assert mod.knowledge_get_source(a)["data"]["deleted_at"] is not None
    assert mod.knowledge_get_source(b)["data"]["deleted_at"] is not None
    hits = _hit_source_ids(mod.knowledge_search("borrow checker", conversation_id="conv-a"))
    assert a not in hits and new_id in hits


# ── proposals ─────────────────────────────────────────────────────────────────


def test_propose_and_save_flow(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    proposed = mod.knowledge_propose(
        title="AI safety findings",
        content="Mechanistic interpretability research maps transformer circuits.",
        scope="global",
        domain_ids=["domain-artificial-intelligence"],
        tags=["research"],
        quality_score=0.8,
        retention_reason="reusable research reference",
    )
    assert proposed["ok"] is True, proposed
    proposal_id = proposed["data"]["proposal_id"]
    saved = mod.knowledge_save(proposal_id)
    assert saved["ok"] is True, saved
    source_id = saved["data"]["source_id"]
    got = mod.knowledge_get_source(source_id)
    assert got["data"]["scope"] == "global"
    assert got["data"]["domain_ids"] == ["domain-artificial-intelligence"]
    assert got["data"]["tags"] == ["research"]
    assert got["data"]["source_quality"] == 0.8
    assert got["data"]["retention_reason"] == "reusable research reference"
    # Proposal resolved; double approval fails.
    assert mod.knowledge_save(proposal_id)["error"]["code"] == "invalid_state"
    conn = _db(tmp_path)
    status = conn.execute(
        "SELECT status FROM knowledge_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()[0]
    conn.close()
    assert status == "approved"


def test_propose_rejects_urls_without_content(mod) -> None:
    result = mod.knowledge_propose(
        title="page", source_urls=["https://example.com"], scope="global"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "url_fetch_unsupported"


def test_unknown_domain_rejected(mod) -> None:
    result = mod.knowledge_ingest(
        title="x", content=RUST_TEXT, scope="global", domain_ids=["domain-nope"]
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_domain"


# ── listing / filtering / pagination ──────────────────────────────────────────


def test_domain_filtering(mod) -> None:
    ai = _ingest(
        mod, "AI notes", RUST_TEXT, scope="global",
        domain_ids=["domain-artificial-intelligence"],
    )
    _ingest(mod, "Finance notes", FINANCE_TEXT, scope="global", domain_ids=["domain-finance"])
    listed = mod.knowledge_list_sources(domain_ids=["domain-finance"])
    ids = [s["source_id"] for s in listed["data"]["sources"]]
    assert ai not in ids and len(ids) == 1
    hits = _hit_source_ids(
        mod.knowledge_search("borrow checker", domain_ids=["domain-finance"])
    )
    assert ai not in hits


def test_tag_and_type_filters(mod) -> None:
    a = _ingest(mod, "Rust notes", RUST_TEXT, scope="global", tags=["lang", "ref"])
    _ingest(mod, "Finance notes", FINANCE_TEXT, scope="global", tags=["money"])
    listed = mod.knowledge_list_sources(tags=["lang", "ref"])
    assert [s["source_id"] for s in listed["data"]["sources"]] == [a]
    assert mod.knowledge_list_sources(source_types=["file"])["data"]["count"] == 0
    assert mod.knowledge_list_sources(source_types=["paste"])["data"]["count"] == 2


def test_keyset_pagination(mod) -> None:
    ids = [_ingest(mod, f"Doc {i}", f"unique content number {i} alpha", scope="global") for i in range(5)]
    page1 = mod.knowledge_list_sources(scope="global", limit=2)
    assert page1["data"]["count"] == 2
    assert page1["data"]["next_cursor"]
    page2 = mod.knowledge_list_sources(scope="global", limit=2, cursor=page1["data"]["next_cursor"])
    page3 = mod.knowledge_list_sources(scope="global", limit=2, cursor=page2["data"]["next_cursor"])
    seen = [
        s["source_id"]
        for s in page1["data"]["sources"] + page2["data"]["sources"] + page3["data"]["sources"]
    ]
    assert page3["data"]["next_cursor"] is None
    assert sorted(seen) == sorted(ids)
