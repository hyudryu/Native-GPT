"""Tests for tools/memory/tool.py."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "memory_tool_under_test"


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


def _write(mod, content: str, scope: str, **kwargs) -> str:
    result = mod.write_memory(content=content, scope=scope, **kwargs)
    assert result["ok"] is True, result
    return result["data"]["memory_id"]


def _hit_ids(result) -> list[str]:
    assert result["ok"] is True, result
    return [hit["memory_id"] for hit in result["data"]["hits"]]


# ── save + recall per scope ─────────────────────────────────────────────────


def test_write_and_get_per_scope(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_project(tmp_path, "proj-1", "conv-1")
    monkeypatch.setenv("AGENTGPT_RUN_ID", "run-1")
    ids = {
        "run": _write(mod, "scratch state for this run", "run"),
        "conversation": _write(mod, "conv fact", "conversation", conversation_id="conv-1"),
        "project": _write(mod, "project decision", "project", project_id="proj-1"),
        "profile": _write(mod, "profile persona fact", "profile", profile_id="prof-1"),
        "user": _write(mod, "user prefers dark mode", "user"),
    }
    for scope, memory_id in ids.items():
        result = mod.get_memory(memory_id)
        assert result["ok"] is True
        assert result["data"]["scope"] == scope
        assert result["data"]["deleted_at"] is None


def test_user_scope_visible_everywhere(mod) -> None:
    memory_id = _write(mod, "user prefers metric units", "user")
    hits = _hit_ids(mod.search_memories("metric units"))
    assert memory_id in hits


def test_run_scope_only_visible_in_own_run(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTGPT_RUN_ID", "run-1")
    memory_id = _write(mod, "run scratch token budget", "run")
    assert memory_id in _hit_ids(mod.search_memories("scratch token"))
    monkeypatch.setenv("AGENTGPT_RUN_ID", "run-2")
    assert memory_id not in _hit_ids(mod.search_memories("scratch token"))


def test_run_scope_requires_run_context(mod) -> None:
    with pytest.raises(mod.MemoryToolError) as excinfo:
        mod.write_memory(content="scratch", scope="run")
    assert excinfo.value.code == "missing_scope_id"


# ── isolation & sharing ─────────────────────────────────────────────────────


def test_conversation_isolation(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a", "conv-b")
    memory_id = _write(
        mod, "conv-a deployment window is Friday", "conversation", conversation_id="conv-a"
    )
    assert memory_id in _hit_ids(
        mod.search_memories("deployment window", conversation_id="conv-a")
    )
    assert memory_id not in _hit_ids(
        mod.search_memories("deployment window", conversation_id="conv-b")
    )


def test_project_sharing_across_conversations(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a", "conv-b")
    memory_id = _write(mod, "project uses pnpm workspaces", "project", conversation_id="conv-a")
    # Visible from the other conversation in the same project.
    assert memory_id in _hit_ids(
        mod.search_memories("pnpm workspaces", conversation_id="conv-b")
    )
    # Not visible from an unrelated project context.
    _seed_project(tmp_path, "proj-2", "conv-c")
    assert memory_id not in _hit_ids(
        mod.search_memories("pnpm workspaces", conversation_id="conv-c")
    )


def test_project_scope_resolves_from_conversation(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    result = mod.write_memory(
        content="project decision via conversation", scope="project", conversation_id="conv-a"
    )
    assert result["ok"] is True
    assert result["data"]["scope_id"] == "proj-1"


# ── keyed upsert & duplicate suppression ────────────────────────────────────


def test_keyed_upsert_supersedes_old_row(mod, tmp_path: Path) -> None:
    first = _write(mod, "editor is vscode", "user", key="editor-preference")
    result = mod.write_memory(content="editor is neovim", scope="user", key="editor-preference")
    assert result["ok"] is True
    second = result["data"]["memory_id"]
    assert result["data"]["duplicate_of"] == first

    old = mod.get_memory(first)["data"]
    assert old["superseded_by"] == second
    assert old["deleted_at"] is not None  # history kept via soft delete

    new = mod.get_memory(second)["data"]
    assert new["content"] == "editor is neovim"
    # Only the new row is searchable.
    hits = _hit_ids(mod.search_memories("editor"))
    assert second in hits
    assert first not in hits


def test_duplicate_suppression_near_identical_content(mod, tmp_path: Path) -> None:
    first = _write(mod, "User prefers dark mode in all editors", "user")
    result = mod.write_memory(content="User prefers dark mode in all editors.", scope="user")
    assert result["ok"] is True
    assert result["data"]["duplicate_of"] == first
    conn = _db(tmp_path)
    active = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE deleted_at IS NULL"
    ).fetchone()["n"]
    conn.close()
    assert active == 1


# ── secret guard ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    [
        "my key is sk-abcdefghijklmnop1234",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
        "api_key = abcdef123456789",
        "password: hunter2",
        "aws id AKIAIOSFODNN7EXAMPLE",
        "token ghp_abcdefghijklmnop123456",
    ],
)
def test_secret_content_rejected(mod, content: str) -> None:
    with pytest.raises(mod.MemoryToolError) as excinfo:
        mod.write_memory(content=content, scope="user")
    assert excinfo.value.code == "sensitive_content_rejected"


def test_secret_guard_applies_on_update(mod) -> None:
    memory_id = _write(mod, "harmless fact", "user")
    with pytest.raises(mod.MemoryToolError) as excinfo:
        mod.update_memory(memory_id, content="password: hunter2")
    assert excinfo.value.code == "sensitive_content_rejected"


# ── filtering ───────────────────────────────────────────────────────────────


def test_expired_memories_excluded(mod) -> None:
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    expired = _write(mod, "temporary launch code freeze", "user", expires_at=past)
    live = _write(mod, "upcoming launch code freeze", "user", expires_at=future)
    hits = _hit_ids(mod.search_memories("launch code freeze"))
    assert expired not in hits
    assert live in hits


def test_sensitivity_filtering(mod) -> None:
    public = _write(mod, "coffee preference public oat latte", "user", sensitivity="public")
    sensitive = _write(mod, "health note sensitive oat latte", "user", sensitivity="sensitive")
    hits = _hit_ids(mod.search_memories("oat latte", sensitivity_maximum="normal"))
    assert public in hits
    assert sensitive not in hits
    hits = _hit_ids(mod.search_memories("oat latte"))
    assert sensitive in hits


def test_soft_delete_excluded_from_search(mod) -> None:
    memory_id = _write(mod, "forgettable penguin fact", "user")
    assert memory_id in _hit_ids(mod.search_memories("penguin"))
    mod.delete_memory(memory_id)
    assert memory_id not in _hit_ids(mod.search_memories("penguin"))
    # Row still exists (soft delete) and is reported as deleted.
    fetched = mod.get_memory(memory_id)
    assert fetched["ok"] is True
    assert fetched["data"]["deleted_at"] is not None


# ── ranking ─────────────────────────────────────────────────────────────────


def test_fts_ranking_relevant_first(mod) -> None:
    _write(mod, "the migration uses sqlite wal mode for concurrency", "user")
    relevant = _write(mod, "sqlite wal mode improves concurrent readers", "user")
    hits = mod.search_memories("sqlite wal concurrent readers")
    ids = _hit_ids(hits)
    assert ids[0] == relevant
    components = hits["data"]["hits"][0]["score_components"]
    assert set(components) == {
        "lexical", "vector", "scope", "importance", "pinned", "recency",
        "frequency", "confidence",
    }


def test_pinned_and_importance_boost_ranking(mod) -> None:
    _write(mod, "release checklist item alpha", "user", importance=0.1)
    pinned = _write(mod, "release checklist item beta", "user", importance=0.1, pinned=True)
    hits = mod.search_memories("release checklist")
    assert _hit_ids(hits)[0] == pinned


def test_search_bumps_access_counters(mod, tmp_path: Path) -> None:
    memory_id = _write(mod, "access counting guinea pig", "user")
    mod.search_memories("guinea pig")
    conn = _db(tmp_path)
    row = conn.execute("SELECT access_count, last_accessed_at FROM memories WHERE id = ?",
                       (memory_id,)).fetchone()
    conn.close()
    assert row["access_count"] == 1
    assert row["last_accessed_at"] is not None


def test_mark_used_bumps_access(mod) -> None:
    memory_id = _write(mod, "manual access bump", "user")
    result = mod.mark_used(memory_id)
    assert result["ok"] is True
    assert result["data"]["access_count"] == 1


# ── proposals ───────────────────────────────────────────────────────────────


def test_proposal_then_write_approval_flow(mod, tmp_path: Path) -> None:
    result = mod.propose_memory("user might prefer concise answers", "user", confidence=0.4)
    assert result["ok"] is True
    proposal_id = result["data"]["proposal_id"]
    assert result["data"]["status"] == "pending"

    written = mod.write_memory(proposal_id=proposal_id)
    assert written["ok"] is True
    assert written["data"]["approved"] is True  # user review approves it
    memory_id = written["data"]["memory_id"]
    assert mod.get_memory(memory_id)["data"]["content"] == "user might prefer concise answers"

    conn = _db(tmp_path)
    proposal = conn.execute(
        "SELECT status, resolved_at FROM memory_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    conn.close()
    assert proposal["status"] == "approved"
    assert proposal["resolved_at"] is not None

    # A resolved proposal cannot be written twice.
    with pytest.raises(mod.MemoryToolError) as excinfo:
        mod.write_memory(proposal_id=proposal_id)
    assert excinfo.value.code == "invalid_state"


def test_direct_user_write_is_unapproved_but_project_write_approved(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    user_result = mod.write_memory(content="global preference", scope="user")
    assert user_result["data"]["approved"] is False
    project_result = mod.write_memory(
        content="project fact", scope="project", project_id="proj-1"
    )
    assert project_result["data"]["approved"] is True


# ── update / promote / merge ────────────────────────────────────────────────


def test_update_moves_scope(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    memory_id = _write(
        mod, "fact moving up", "conversation", conversation_id="conv-a"
    )
    result = mod.update_memory(memory_id, scope="project")
    assert result["ok"] is True
    assert result["data"]["scope"] == "project"
    assert result["data"]["scope_id"] == "proj-1"
    assert result["data"]["moved"] is True


def test_promotion_chain(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    memory_id = _write(mod, "growing fact", "conversation", conversation_id="conv-a")
    promoted = mod.promote_memory(memory_id, "project")
    assert promoted["ok"] is True
    assert promoted["data"]["scope_id"] == "proj-1"
    promoted = mod.promote_memory(memory_id, "user")
    assert promoted["ok"] is True
    assert promoted["data"]["scope_id"] == "user"
    # Demotion is rejected.
    with pytest.raises(mod.MemoryToolError) as excinfo:
        mod.promote_memory(memory_id, "conversation")
    assert excinfo.value.code == "invalid_promotion"


def test_merge_combines_and_supersedes(mod, tmp_path: Path) -> None:
    first = _write(mod, "deploys happen on fridays", "user", tags=["deploy"])
    second = _write(mod, "deploys use blue-green strategy", "user", tags=["release"])
    result = mod.merge_memories([first, second], "deploys are friday blue-green releases")
    assert result["ok"] is True
    merged_id = result["data"]["memory_id"]
    merged = mod.get_memory(merged_id)["data"]
    assert merged["content"] == "deploys are friday blue-green releases"
    assert sorted(merged["tags"]) == ["deploy", "release"]
    for source in (first, second):
        row = mod.get_memory(source)["data"]
        assert row["superseded_by"] == merged_id
        assert row["deleted_at"] is not None
    hits = _hit_ids(mod.search_memories("blue-green deploys"))
    assert merged_id in hits
    assert first not in hits and second not in hits


def test_merge_scope_mismatch_requires_destination(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a", "conv-b")
    first = _write(mod, "fact one", "conversation", conversation_id="conv-a")
    second = _write(mod, "fact two", "conversation", conversation_id="conv-b")
    with pytest.raises(mod.MemoryToolError) as excinfo:
        mod.merge_memories([first, second], "merged facts")
    assert excinfo.value.code == "scope_mismatch"
    resolved = mod.merge_memories([first, second], "merged facts", destination_scope="user")
    assert resolved["ok"] is True


# ── listing & pagination ────────────────────────────────────────────────────


def test_list_keyset_pagination(mod) -> None:
    topics = ["anteater", "badger", "cheetah", "dolphin", "elephant"]
    for topic in topics:
        _write(mod, f"listable fact about the {topic}", "user")
    first_page = mod.list_memories(scopes=["user"], limit=2)
    assert first_page["ok"] is True
    assert first_page["data"]["count"] == 2
    cursor = first_page["data"]["next_cursor"]
    assert cursor
    seen = {m["memory_id"] for m in first_page["data"]["memories"]}
    while cursor:
        page = mod.list_memories(scopes=["user"], limit=2, cursor=cursor)
        assert page["ok"] is True
        for memory in page["data"]["memories"]:
            assert memory["memory_id"] not in seen  # no overlap between pages
            seen.add(memory["memory_id"])
        cursor = page["data"]["next_cursor"]
    assert len(seen) == 5


def test_list_filters(mod, tmp_path: Path) -> None:
    _seed_project(tmp_path, "proj-1", "conv-a")
    pinned = _write(mod, "pinned tagged fact", "user", tags=["work"], pinned=True)
    _write(mod, "unpinned other fact", "user", tags=["home"])
    result = mod.list_memories(tags=["work"], pinned=True)
    ids = [m["memory_id"] for m in result["data"]["memories"]]
    assert ids == [pinned]
    project = _write(mod, "project scoped fact", "project", project_id="proj-1")
    result = mod.list_memories(scopes=["project"], project_id="proj-1")
    assert [m["memory_id"] for m in result["data"]["memories"]] == [project]


# ── validation ──────────────────────────────────────────────────────────────


def test_validation_errors(mod) -> None:
    def code_of(fn, *args, **kwargs) -> str:
        with pytest.raises(mod.MemoryToolError) as excinfo:
            fn(*args, **kwargs)
        return excinfo.value.code

    assert code_of(mod.write_memory, content="", scope="user") == "validation_error"
    assert code_of(mod.write_memory, content="fact", scope="galaxy") == "validation_error"
    assert code_of(mod.search_memories, "x", scopes=["galaxy"]) == "validation_error"
    assert code_of(mod.get_memory, "mem_missing") == "not_found"
    assert code_of(mod.delete_memory, "mem_missing") == "not_found"
    assert code_of(mod.list_memories, limit=0) == "validation_error"
    bad_expiry = code_of(mod.write_memory, content="fact", scope="user", expires_at="not-a-date")
    assert bad_expiry == "validation_error"


def test_tool_export_lists_all_functions(mod) -> None:
    names = [getattr(t, "__name__", "") for t in mod.TOOL]
    for expected in (
        "memory_search", "memory_propose", "memory_write", "memory_get",
        "memory_update", "memory_delete", "memory_list", "memory_promote",
        "memory_merge", "memory_mark_used",
    ):
        assert expected in names
