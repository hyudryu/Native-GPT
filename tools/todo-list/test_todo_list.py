"""Tests for tools/todo-list/tool.py."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testdb import create_test_db  # noqa: E402
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "todo_list_tool_under_test"


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


def _make_plan(mod, steps=None, **kwargs):
    result = mod.create_plan(
        "ship the feature",
        ["tests pass", "docs updated"],
        steps=steps,
        **kwargs,
    )
    assert result["ok"] is True, result
    return result["data"]["plan_id"]


# ── creation ──────────────────────────────────────────────────────────────


def test_create_without_steps_is_draft(mod) -> None:
    result = mod.create_plan("goal", ["criterion"])
    assert result["ok"] is True
    assert result["data"]["status"] == "draft"
    assert result["data"]["step_ids"] == []


def test_create_with_steps_is_ready(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "first"}, {"title": "second"}])
    fetched = mod.get_plan(plan_id)
    assert fetched["data"]["status"] == "ready"
    steps = fetched["data"]["steps"]
    assert [s["title"] for s in steps] == ["first", "second"]
    assert all(s["status"] == "ready" for s in steps)


def test_create_requires_goal_and_criteria(mod) -> None:
    with pytest.raises(mod.TodoError):
        mod.create_plan("", ["c"])
    with pytest.raises(mod.TodoError):
        mod.create_plan("goal", [])


def test_create_rejects_bad_dependency_ref(mod) -> None:
    with pytest.raises(mod.TodoError):
        mod.create_plan("g", ["c"], steps=[{"title": "a", "dependencies": [7]}])


# ── dependencies & readiness ──────────────────────────────────────────────


def test_dependency_gates_readiness(mod) -> None:
    plan_id = _make_plan(
        mod,
        steps=[
            {"title": "research"},
            {"title": "write", "dependencies": [0]},
        ],
    )
    ready = mod.next_ready_steps(plan_id)
    assert [s["title"] for s in ready["data"]["ready"]] == ["research"]

    fetched = mod.get_plan(plan_id)
    research, write = fetched["data"]["steps"]
    assert write["status"] == "pending"

    mod.update_step(plan_id, research["id"], status="in_progress")
    mod.update_step(plan_id, research["id"], status="completed", result_summary="done")

    ready = mod.next_ready_steps(plan_id)
    assert [s["title"] for s in ready["data"]["ready"]] == ["write"]
    fetched = mod.get_plan(plan_id)
    assert fetched["data"]["steps"][1]["status"] == "ready"


def test_dependency_by_title(mod) -> None:
    plan_id = _make_plan(
        mod,
        steps=[{"title": "a"}, {"title": "b", "dependencies": ["a"]}],
    )
    fetched = mod.get_plan(plan_id)
    a, b = fetched["data"]["steps"]
    assert b["dependencies"] == [a["id"]]


# ── lifecycle & transitions ───────────────────────────────────────────────


def test_full_lifecycle_auto_completes_plan(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "only"}])
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    result = mod.update_step(plan_id, step["id"], status="in_progress")
    assert result["data"]["plan_status"] == "running"
    result = mod.update_step(
        plan_id, step["id"], status="completed", result_summary="shipped"
    )
    assert result["data"]["plan_status"] == "completed"
    fetched = mod.get_plan(plan_id)
    assert fetched["data"]["status"] == "completed"
    assert fetched["data"]["completed_at"] is not None
    assert fetched["data"]["progress"]["percent_complete"] == 100


def test_invalid_transition_rejected(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "s"}, {"title": "other"}])
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step["id"], status="completed")
    with pytest.raises(mod.TodoError, match="cannot move step"):
        mod.update_step(plan_id, step["id"], status="in_progress")


def test_failed_attempts_block_step_at_maximum(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "flaky", "maximum_attempts": 2}])
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step["id"], status="in_progress")
    result = mod.update_step(plan_id, step["id"], status="failed")
    assert result["data"]["step_status"] == "failed"
    # Retry once more: second failure exhausts the attempt budget.
    mod.update_step(plan_id, step["id"], status="in_progress")
    result = mod.update_step(plan_id, step["id"], status="failed")
    assert result["data"]["step_status"] == "blocked"
    assert result["data"]["plan_status"] == "blocked"
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    assert step["attempts"] == 2


def test_complete_refused_while_steps_remain(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}, {"title": "b"}])
    step_a = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step_a["id"], status="completed")
    with pytest.raises(mod.TodoError, match="not finished"):
        mod.complete_plan(plan_id, "done")


def test_complete_success(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}, {"title": "b"}])
    for step in mod.get_plan(plan_id)["data"]["steps"]:
        mod.update_step(plan_id, step["id"], status="completed")
    result = mod.complete_plan(plan_id, "all done", evidence_refs=["file:report.md"])
    assert result["ok"] is True
    assert result["data"]["status"] == "completed"


# ── revision ──────────────────────────────────────────────────────────────


def test_revise_add_update_remove(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "keep"}, {"title": "drop"}])
    keep, drop = mod.get_plan(plan_id)["data"]["steps"]
    result = mod.revise_plan(
        plan_id,
        "scope changed",
        steps_to_add=[{"title": "new work", "dependencies": [keep["id"]]}],
        steps_to_update=[{"step_id": keep["id"], "objective": "refined"}],
        steps_to_remove=[drop["id"]],
    )
    assert result["ok"] is True
    assert len(result["data"]["changed"]["added"]) == 1
    fetched = mod.get_plan(plan_id)
    titles = {s["title"]: s for s in fetched["data"]["steps"]}
    assert titles["drop"]["status"] == "cancelled"
    assert titles["keep"]["objective"] == "refined"
    assert titles["new work"]["status"] == "pending"  # waits on "keep"


def test_revise_completed_plan_rejected(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}])
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step["id"], status="completed")
    with pytest.raises(mod.TodoError, match="cannot revise"):
        mod.revise_plan(plan_id, "too late")


def test_revise_dependency_changes(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}, {"title": "b"}])
    a, b = mod.get_plan(plan_id)["data"]["steps"]
    mod.revise_plan(
        plan_id, "b must wait on a",
        dependency_changes=[{"step_id": b["id"], "dependencies": [a["id"]]}],
    )
    fetched = mod.get_plan(plan_id)
    assert fetched["data"]["steps"][1]["dependencies"] == [a["id"]]
    assert fetched["data"]["steps"][1]["status"] == "pending"


# ── pause / resume / cancel ───────────────────────────────────────────────


def test_pause_resume(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}])
    result = mod._set_plan_status(plan_id, "paused", "plan_paused")
    assert result["data"]["status"] == "paused"
    result = mod._set_plan_status(plan_id, "ready", "plan_resumed")
    assert result["data"]["status"] == "ready"


def test_cancel_marks_unfinished_steps(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}, {"title": "b"}])
    step_a = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step_a["id"], status="completed")
    result = mod._set_plan_status(plan_id, "cancelled", "plan_cancelled")
    assert result["data"]["status"] == "cancelled"
    steps = {s["title"]: s["status"] for s in mod.get_plan(plan_id)["data"]["steps"]}
    assert steps == {"a": "completed", "b": "cancelled"}


def test_pause_completed_plan_rejected(mod) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}])
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step["id"], status="completed")
    with pytest.raises(mod.TodoError, match="cannot move plan"):
        mod._set_plan_status(plan_id, "paused", "plan_paused")


# ── listing & events ──────────────────────────────────────────────────────


def test_list_filters_and_pagination(mod) -> None:
    for i in range(3):
        _make_plan(mod, steps=[{"title": f"s{i}"}], conversation_id=f"conv-{i}")
    result = mod.list_plans(status="ready", limit=2)
    assert result["data"]["count"] == 2
    assert result["data"]["next_cursor"] is not None
    page2 = mod.list_plans(status="ready", limit=2, cursor=result["data"]["next_cursor"])
    assert page2["data"]["count"] == 1
    assert page2["data"]["next_cursor"] is None
    by_conv = mod.list_plans(conversation_id="conv-1")
    assert by_conv["data"]["count"] == 1


def test_events_recorded(mod, tmp_path: Path) -> None:
    plan_id = _make_plan(mod, steps=[{"title": "a"}])
    step = mod.get_plan(plan_id)["data"]["steps"][0]
    mod.update_step(plan_id, step["id"], status="completed")
    conn = _db(tmp_path)
    try:
        rows = conn.execute(
            "SELECT event_type FROM plan_events WHERE plan_id = ? ORDER BY created_at, rowid",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    types = [r["event_type"] for r in rows]
    assert "plan_created" in types
    assert "step_added" in types
    assert "step_status" in types
    assert "plan_status" in types  # auto-completion


# ── run context ───────────────────────────────────────────────────────────


def test_context_ids_default_from_runtime(mod, tmp_path: Path) -> None:
    from agentgpt_runtime import run_context

    run_context.set_run_context("run-1", "conv-1")
    try:
        result = mod.create_plan("goal", ["c"])
    finally:
        run_context.clear_run_context()
    assert result["data"]["run_id"] == "run-1"
    assert result["data"]["conversation_id"] == "conv-1"


def test_context_env_fallback(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTGPT_RUN_ID", "run-env")
    monkeypatch.setenv("AGENTGPT_CONVERSATION_ID", "conv-env")
    result = mod.create_plan("goal", ["c"])
    assert result["data"]["run_id"] == "run-env"
    assert result["data"]["conversation_id"] == "conv-env"


def test_project_resolved_from_conversation(mod, tmp_path: Path) -> None:
    conn = _db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at)"
            " VALUES ('proj-1', 'P', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO conversations (id, project_id, title, created_at, updated_at)"
            " VALUES ('conv-1', 'proj-1', 't', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    result = mod.create_plan("goal", ["c"], conversation_id="conv-1")
    assert result["data"]["project_id"] == "proj-1"


def test_tools_exported_as_list(mod) -> None:
    assert isinstance(mod.TOOL, list)
    names = {t.tool_name for t in mod.TOOL}
    assert {
        "todo_create", "todo_get", "todo_list", "todo_add_step", "todo_update_step",
        "todo_revise", "todo_pause", "todo_resume", "todo_cancel", "todo_complete",
        "todo_next_ready_steps",
    } == names
