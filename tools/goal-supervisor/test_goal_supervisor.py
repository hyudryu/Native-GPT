"""Tests for tools/goal-supervisor/tool.py."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "goal_supervisor_tool_under_test"


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


def _make_contract(mod, criteria=None, **kwargs):
    result = mod.create_contract(
        "write a market report",
        "research",
        criteria if criteria is not None else [{"type": "answer_nonempty"}],
        **kwargs,
    )
    assert result["ok"] is True, result
    return result["data"]["contract_id"]


# ── contract creation ─────────────────────────────────────────────────────


def test_create_contract_validates_criteria(mod) -> None:
    with pytest.raises(mod.GoalError):
        mod.create_contract("goal", "research", [])
    with pytest.raises(mod.GoalError):
        mod.create_contract("goal", "research", [{"no_type": True}])
    with pytest.raises(mod.GoalError):
        mod.create_contract("", "research", [{"type": "answer_nonempty"}])


def test_create_contract_stores_budgets(mod) -> None:
    contract_id = _make_contract(
        mod, budgets={"maximum_recovery_attempts": 1, "maximum_iterations": 5}
    )
    status = mod.get_status(contract_id)
    assert status["data"]["budgets"]["maximum_recovery_attempts"] == 1
    assert status["data"]["status"] == "active"


# ── evaluation: evidence-based validators ─────────────────────────────────


def test_answer_nonempty_pass_and_fail(mod) -> None:
    contract_id = _make_contract(mod)
    result = mod.evaluate(contract_id, {"answer_text": "a real answer"})
    assert result["data"]["passed"] is True
    result = mod.evaluate(contract_id, {"answer_text": "   "})
    assert result["data"]["passed"] is False
    assert result["data"]["results"][0]["detail"]["reason"] == "empty_answer"


def test_missing_evidence_fails(mod) -> None:
    contract_id = _make_contract(mod)
    result = mod.evaluate(contract_id)  # no evidence at all
    assert result["data"]["passed"] is False
    assert result["data"]["results"][0]["detail"]["reason"] == "missing_evidence"


def test_minimum_response_characters(mod) -> None:
    contract_id = _make_contract(
        mod, [{"type": "minimum_response_characters", "value": 10}]
    )
    assert mod.evaluate(contract_id, {"answer_text": "short"})["data"]["passed"] is False
    assert mod.evaluate(contract_id, {"answer_text": "long enough answer"})["data"]["passed"] is True


def test_tool_call_validators(mod) -> None:
    contract_id = _make_contract(
        mod,
        [
            {"type": "required_tool_called", "tool": "web_search"},
            {"type": "minimum_successful_tool_calls", "value": 2},
        ],
    )
    evidence = {
        "tool_calls": [
            {"name": "web_search", "ok": True},
            {"name": "read_file", "ok": True},
            {"name": "write_file", "ok": False},
        ]
    }
    result = mod.evaluate(contract_id, evidence)
    assert result["data"]["passed"] is True
    evidence["tool_calls"][1]["ok"] = False
    result = mod.evaluate(contract_id, evidence)
    assert result["data"]["passed"] is False
    assert result["data"]["results"][1]["passed"] is False


def test_minimum_sources_validators(mod) -> None:
    contract_id = _make_contract(
        mod,
        [
            {"type": "minimum_sources", "value": 3},
            {"type": "minimum_successful_sources", "value": 2},
        ],
    )
    evidence = {
        "sources": [
            {"url": "a", "ok": True},
            {"url": "b", "status": "failed"},
            {"url": "c"},
        ]
    }
    result = mod.evaluate(contract_id, evidence)
    assert result["data"]["passed"] is True
    result = mod.evaluate(contract_id, {"sources": [{"url": "a"}]})
    assert result["data"]["passed"] is False


def test_json_schema_valid(mod) -> None:
    contract_id = _make_contract(
        mod,
        [
            {
                "type": "json_schema_valid",
                "schema": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"count": {"type": "integer"}},
                },
            }
        ],
    )
    good = mod.evaluate(contract_id, {"json_text": '{"title": "t", "count": 3}'})
    assert good["data"]["passed"] is True
    bad = mod.evaluate(contract_id, {"json_text": '{"count": "three"}'})
    assert bad["data"]["passed"] is False
    problems = bad["data"]["results"][0]["detail"]["problems"]
    assert any("title" in p for p in problems)
    invalid = mod.evaluate(contract_id, {"json_text": "not json"})
    assert invalid["data"]["passed"] is False


# ── evaluation: local-state validators ────────────────────────────────────


def test_file_validators(mod, tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    target.write_text("v1", encoding="utf-8")
    v1_hash = hashlib.sha256(b"v1").hexdigest()
    contract_id = _make_contract(
        mod,
        [
            {"type": "file_exists", "path": "out.md"},
            {"type": "file_hash_changed", "path": "out.md", "previous_sha256": v1_hash},
        ],
    )
    result = mod.evaluate(contract_id, {})
    assert result["data"]["results"][0]["passed"] is True
    assert result["data"]["results"][1]["passed"] is False  # hash unchanged
    target.write_text("v2", encoding="utf-8")
    result = mod.evaluate(contract_id, {})
    assert result["data"]["passed"] is True


def test_file_exists_rejects_traversal(mod) -> None:
    contract_id = _make_contract(mod, [{"type": "file_exists", "path": "../../etc/passwd"}])
    result = mod.evaluate(contract_id, {})
    assert result["data"]["passed"] is False


def test_artifact_exists(mod, tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO artifacts (id, name, mime_type, storage_path, created_at, updated_at)"
            " VALUES ('art-1', 'report.md', 'text/markdown', 'x', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    contract_id = _make_contract(
        mod, [{"type": "artifact_exists", "artifact_id": "art-1"}]
    )
    assert mod.evaluate(contract_id, {})["data"]["passed"] is True
    missing = _make_contract(
        mod, [{"type": "artifact_exists", "artifact_id": "art-nope"}]
    )
    assert mod.evaluate(missing, {})["data"]["passed"] is False


def test_plan_steps_completed(mod, tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO plans (id, goal, status, created_at, updated_at)"
            " VALUES ('plan-1', 'g', 'running', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO plan_steps (id, plan_id, position, title, status, created_at, updated_at)"
            " VALUES ('s1', 'plan-1', 0, 'a', 'completed', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO plan_steps (id, plan_id, position, title, status, created_at, updated_at)"
            " VALUES ('s2', 'plan-1', 1, 'b', 'in_progress', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    contract_id = _make_contract(
        mod, [{"type": "plan_steps_completed", "plan_id": "plan-1"}]
    )
    assert mod.evaluate(contract_id, {})["data"]["passed"] is False
    conn = _db(tmp_path)
    try:
        conn.execute("UPDATE plan_steps SET status = 'skipped' WHERE id = 's2'")
        conn.commit()
    finally:
        conn.close()
    assert mod.evaluate(contract_id, {})["data"]["passed"] is True


def test_knowledge_source_and_memory_validators(mod, tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO knowledge_sources (id, title, source_type, content, created_at, updated_at)"
            " VALUES ('ks-1', 'Ref Doc', 'paste', 'text', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO memories (id, scope, memory_key, content, created_at, updated_at)"
            " VALUES ('mem-1', 'global', 'user-pref', 'likes tea', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    contract_id = _make_contract(
        mod,
        [
            {"type": "knowledge_source_exists", "source_id": "ks-1"},
            {"type": "memory_record_exists", "memory_key": "user-pref"},
        ],
    )
    assert mod.evaluate(contract_id, {})["data"]["passed"] is True
    conn = _db(tmp_path)
    try:
        conn.execute("UPDATE knowledge_sources SET enabled = 0 WHERE id = 'ks-1'")
        conn.commit()
    finally:
        conn.close()
    result = mod.evaluate(contract_id, {})
    assert result["data"]["results"][0]["passed"] is False
    assert result["data"]["results"][1]["passed"] is True


def test_unknown_validator_fails(mod) -> None:
    contract_id = _make_contract(mod, [{"type": "vibes_check"}])
    result = mod.evaluate(contract_id, {})
    assert result["data"]["passed"] is False
    assert result["data"]["results"][0]["detail"]["reason"] == "unknown_validator"


# ── progress, status, recovery ────────────────────────────────────────────


def test_record_progress_and_status(mod) -> None:
    contract_id = _make_contract(mod)
    result = mod.record_progress(
        contract_id,
        completed_items=["gathered sources"],
        remaining_items=["write draft", "verify"],
        evidence_refs=["file:sources.md"],
    )
    assert result["data"]["progress"]["completed_items"] == ["gathered sources"]
    status = mod.get_status(contract_id)
    assert status["data"]["progress"]["remaining_items"] == ["write draft", "verify"]
    assert len(status["data"]["progress"]["history"]) == 1


def test_recovery_budget_enforced(mod) -> None:
    contract_id = _make_contract(mod, budgets={"maximum_recovery_attempts": 2})
    mod.record_progress(contract_id, completed_items=["a"], remaining_items=["b"])
    first = mod.request_recovery(contract_id, "search API down")
    assert first["ok"] is True
    assert "GOAL SUPERVISOR — RECOVERY ATTEMPT 1/2" in first["data"]["recovery_prompt"]
    assert "write a market report" in first["data"]["recovery_prompt"]
    assert "; b" in first["data"]["recovery_prompt"] or "b" in first["data"]["recovery_prompt"]
    second = mod.request_recovery(contract_id, "still down")
    assert second["ok"] is True
    third = mod.request_recovery(contract_id, "third strike")
    assert third["ok"] is False
    assert third["error"]["code"] == "budget_exhausted"
    assert third["data"]["advice"] == "goal_mark_blocked"


def test_mark_blocked(mod) -> None:
    contract_id = _make_contract(mod)
    result = mod.mark_blocked(contract_id, "provider unavailable", ["retried twice"])
    assert result["data"]["status"] == "blocked"
    status = mod.get_status(contract_id)
    assert status["data"]["blocked_reason"] == "provider unavailable"
    with pytest.raises(mod.GoalError, match="progress is frozen"):
        mod.record_progress(contract_id, completed_items=["x"])


def test_mark_complete_requires_passing_validators(mod) -> None:
    contract_id = _make_contract(mod)
    with pytest.raises(mod.GoalError, match="run goal_evaluate first"):
        mod.mark_complete(contract_id)
    mod.evaluate(contract_id, {"answer_text": "done"})
    result = mod.mark_complete(contract_id)
    assert result["data"]["status"] == "completed"
    assert result["data"]["verification_source"] == "stored"


def test_mark_complete_refused_when_failing(mod) -> None:
    contract_id = _make_contract(mod)
    mod.evaluate(contract_id, {})  # missing evidence → failing
    with pytest.raises(mod.GoalError, match="not passing"):
        mod.mark_complete(contract_id)


def test_mark_complete_with_supplied_results(mod) -> None:
    contract_id = _make_contract(mod)
    result = mod.mark_complete(
        contract_id, validation_results=[{"index": 0, "passed": True}]
    )
    assert result["data"]["verification_source"] == "supplied"


def test_get_status_tracks_latest_validation(mod) -> None:
    contract_id = _make_contract(mod)
    mod.evaluate(contract_id, {})  # fail
    mod.evaluate(contract_id, {"answer_text": "ok"})  # pass
    status = mod.get_status(contract_id)
    summary = status["data"]["validation_summary"]
    assert summary == {"criteria": 1, "evaluated": 1, "passing": 1}
    assert status["data"]["latest_validations"][0]["passed"] is True


# ── context fallback & exports ────────────────────────────────────────────


def test_context_env_fallback(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTGPT_RUN_ID", "run-env")
    monkeypatch.setenv("AGENTGPT_CONVERSATION_ID", "conv-env")
    contract_id = _make_contract(mod)
    status = mod.get_status(contract_id)
    assert status["data"]["run_id"] == "run-env"
    assert status["data"]["conversation_id"] == "conv-env"


def test_tools_exported_as_list(mod) -> None:
    assert isinstance(mod.TOOL, list)
    names = {t.tool_name for t in mod.TOOL}
    assert names == {
        "goal_create_contract", "goal_get_status", "goal_record_progress",
        "goal_evaluate", "goal_request_recovery", "goal_mark_blocked",
        "goal_mark_complete",
    }
