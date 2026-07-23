"""Todo List Strands tools — the agent's task tracker for multi-step work.

Multi-tool folder: `TOOL` is a list of Strands tools (see the registry's
multi-tool support). State lives in the app database (`plans`, `plan_steps`,
`plan_events` — migration 0011), opened through `tools/_lib/db.py`; run and
conversation ids default from `tools/_lib/context.py` when the caller omits
them. Both `_lib` modules are loaded by file path because the runtime
imports each tool.py as a standalone module (no package context).

Status model
------------
Plans:   draft → ready → running → completed / failed / blocked / cancelled
         (paused interrupts any non-terminal state)
Steps:   pending → ready → in_progress → completed / failed / skipped /
         blocked / cancelled. A step becomes `ready` when every step id in
         its dependencies is `completed` (or `skipped`). Marking a step
         `failed` increments `attempts`; once attempts reach
         `maximum_attempts` the step is auto-marked `blocked`.

Every mutation records a row in `plan_events` so the run can be audited.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import uuid
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
_context = _load_lib("context.py", "agentgpt_tools_context")

PLAN_STATUSES = (
    "draft", "ready", "running", "paused", "blocked", "failed", "completed", "cancelled",
)
STEP_STATUSES = (
    "pending", "ready", "in_progress", "blocked", "failed", "completed", "skipped", "cancelled",
)
TERMINAL_STEP_STATUSES = frozenset({"completed", "skipped", "cancelled"})
# Dependencies satisfied by these statuses unblock their dependents.
DEP_SATISFIED_STATUSES = frozenset({"completed", "skipped"})
# Dependency statuses that can never produce a result block dependents.
DEP_DOOMED_STATUSES = frozenset({"cancelled", "failed", "blocked"})

# Explicit transitions allowed via todo_update_step. Readiness promotions
# (pending -> ready) additionally happen automatically after any change.
_STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"ready", "in_progress", "blocked", "skipped", "cancelled"}),
    "ready": frozenset({"in_progress", "completed", "failed", "blocked", "skipped", "cancelled"}),
    "in_progress": frozenset({"completed", "failed", "blocked", "skipped", "cancelled"}),
    "failed": frozenset({"ready", "in_progress", "completed", "blocked", "skipped", "cancelled"}),
    "blocked": frozenset({"ready", "in_progress", "completed", "skipped", "cancelled"}),
    "completed": frozenset(),
    "skipped": frozenset(),
    "cancelled": frozenset(),
}


class TodoError(ValueError):
    """Any todo-list failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _parse_json(raw: str | None, default: Any = None) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _ok(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TodoError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise TodoError("db_unavailable", str(exc)) from exc


def _scope_defaults(
    conn: sqlite3.Connection,
    project_id: str | None,
    conversation_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Fill project/conversation/run ids from the run context and DB."""
    ctx = _context.get_run_context()
    run_id = ctx.get("run_id")
    if not conversation_id:
        conversation_id = ctx.get("conversation_id")
    if not project_id and conversation_id:
        project_id = _db.project_id_for_conversation(conn, conversation_id)
    return project_id, conversation_id, run_id


def _fetch_plan(conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise TodoError("not_found", f"plan not found: {plan_id}")
    return row


def _fetch_steps(conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY position, created_at, id",
        (plan_id,),
    ).fetchall()


def _record_event(
    conn: sqlite3.Connection,
    plan_id: str,
    step_id: str | None,
    event_type: str,
    payload: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO plan_events (id, plan_id, step_id, event_type, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_new_id("evt"), plan_id, step_id, event_type, _json(payload), _now()),
    )


def _step_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "position": row["position"],
        "title": row["title"],
        "objective": row["objective"],
        "status": row["status"],
        "dependencies": _parse_json(row["dependencies_json"], []),
        "required_capabilities": _parse_json(row["required_capabilities_json"], []),
        "success_criteria": _parse_json(row["success_criteria_json"], []),
        "maximum_attempts": row["maximum_attempts"],
        "attempts": row["attempts"],
        "result_summary": row["result_summary"],
        "evidence_refs": _parse_json(row["evidence_refs_json"], []),
        "failure": _parse_json(row["failure_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _plan_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "conversation_id": row["conversation_id"],
        "project_id": row["project_id"],
        "goal": row["goal"],
        "mode": row["mode"],
        "status": row["status"],
        "success_criteria": _parse_json(row["success_criteria_json"], []),
        "constraints": _parse_json(row["constraints_json"]),
        "budget": _parse_json(row["budget_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "failure": _parse_json(row["failure_json"]),
    }


def _progress(steps: list[sqlite3.Row]) -> dict[str, Any]:
    counts = {status: 0 for status in STEP_STATUSES}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    total = len(steps)
    done = counts["completed"] + counts["skipped"]
    return {
        "total": total,
        "counts": counts,
        "completed": counts["completed"],
        "remaining": total - done - counts["cancelled"],
        "percent_complete": round(100 * done / total) if total else 0,
    }


def _recompute_readiness(conn: sqlite3.Connection, plan_id: str) -> None:
    """Sync pending/ready steps with their dependencies.

    Promotes pending steps whose dependencies all resolved, blocks steps
    with a doomed dependency (failed/blocked/cancelled), and demotes ready
    steps that gained an unfinished dependency via revision.
    """
    steps = _fetch_steps(conn, plan_id)
    by_id = {step["id"]: step for step in steps}
    now = _now()
    for step in steps:
        if step["status"] not in ("pending", "ready"):
            continue
        deps = _parse_json(step["dependencies_json"], [])
        dep_statuses = [by_id[d]["status"] for d in deps if d in by_id]
        if any(status in DEP_DOOMED_STATUSES for status in dep_statuses):
            conn.execute(
                "UPDATE plan_steps SET status = 'blocked', failure_json = ?, updated_at = ?"
                " WHERE id = ?",
                (_json({"reason": "dependency cannot complete"}), now, step["id"]),
            )
            _record_event(
                conn, plan_id, step["id"], "step_blocked",
                {"reason": "a dependency failed, was blocked, or was cancelled"},
            )
        elif all(status in DEP_SATISFIED_STATUSES for status in dep_statuses):
            if step["status"] == "pending":
                conn.execute(
                    "UPDATE plan_steps SET status = 'ready', updated_at = ? WHERE id = ?",
                    (now, step["id"]),
                )
                _record_event(conn, plan_id, step["id"], "step_ready", None)
        elif step["status"] == "ready":
            # A revision added or reopened a dependency: back to waiting.
            conn.execute(
                "UPDATE plan_steps SET status = 'pending', updated_at = ? WHERE id = ?",
                (now, step["id"]),
            )
            _record_event(
                conn, plan_id, step["id"], "step_pending",
                {"reason": "dependency added or reopened"},
            )


def _recompute_plan_status(conn: sqlite3.Connection, plan_id: str) -> str:
    """Derive plan status from its steps; returns the (possibly new) status."""
    plan = _fetch_plan(conn, plan_id)
    status = plan["status"]
    if status in ("completed", "cancelled", "paused"):
        return status
    steps = _fetch_steps(conn, plan_id)
    if not steps:
        return status
    statuses = [step["status"] for step in steps]
    now = _now()
    new_status = status
    if all(s in TERMINAL_STEP_STATUSES for s in statuses):
        new_status = "completed"
    elif not any(s in ("pending", "ready", "in_progress") for s in statuses):
        # Nothing left that can advance: everything is blocked or failed.
        new_status = "blocked"
    elif any(s == "in_progress" for s in statuses):
        new_status = "running"
    if new_status != status:
        completed_at = now if new_status == "completed" else plan["completed_at"]
        started_at = plan["started_at"] or (now if new_status == "running" else None)
        conn.execute(
            "UPDATE plans SET status = ?, completed_at = ?, started_at = ?, updated_at = ?"
            " WHERE id = ?",
            (new_status, completed_at, started_at, now, plan_id),
        )
        _record_event(
            conn, plan_id, None, "plan_status",
            {"from": status, "to": new_status},
        )
    return new_status


def _normalize_criteria(value: Any, field: str) -> list[Any]:
    """Accept a list or a single string for criteria/constraints fields."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    raise TodoError("validation_error", f"{field} must be a list or a string")


def _insert_step(
    conn: sqlite3.Connection,
    plan_id: str,
    position: int,
    spec: dict[str, Any],
) -> str:
    """Insert one plan step from a spec dict; returns the new step id."""
    title = _require_text(spec.get("title"), "step title")
    objective = spec.get("objective")
    if objective is not None and not isinstance(objective, str):
        raise TodoError("validation_error", "step objective must be a string")
    maximum_attempts = spec.get("maximum_attempts", 2)
    try:
        maximum_attempts = int(maximum_attempts)
    except (TypeError, ValueError) as exc:
        raise TodoError("validation_error", "maximum_attempts must be an integer") from exc
    if maximum_attempts < 1:
        raise TodoError("validation_error", "maximum_attempts must be >= 1")
    step_id = _new_id("step")
    now = _now()
    conn.execute(
        "INSERT INTO plan_steps (id, plan_id, position, title, objective, status,"
        " dependencies_json, required_capabilities_json, success_criteria_json,"
        " maximum_attempts, attempts, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 0, ?, ?)",
        (
            step_id,
            plan_id,
            position,
            title,
            objective,
            _json(spec.get("dependencies") or []),
            _json(_normalize_criteria(spec.get("required_capabilities"), "required_capabilities")),
            _json(_normalize_criteria(spec.get("success_criteria"), "step success_criteria")),
            maximum_attempts,
            now,
            now,
        ),
    )
    _record_event(conn, plan_id, step_id, "step_added", {"title": title, "position": position})
    return step_id


def _resolve_create_dependencies(
    conn: sqlite3.Connection,
    plan_id: str,
    step_ids: list[str],
    specs: list[dict[str, Any]],
) -> None:
    """Map create-time dependency refs (0-based index or title) to step ids."""
    now = _now()
    for position, spec in enumerate(specs):
        refs = spec.get("dependencies") or []
        if not refs:
            continue
        resolved: list[str] = []
        for ref in refs:
            if isinstance(ref, int):
                if ref < 0 or ref >= len(step_ids):
                    raise TodoError(
                        "validation_error",
                        f"step {position}: dependency index {ref} out of range",
                    )
                resolved.append(step_ids[ref])
            elif isinstance(ref, str):
                matches = [
                    step_ids[i]
                    for i, s in enumerate(specs)
                    if s.get("title") == ref
                ]
                if not matches:
                    raise TodoError(
                        "validation_error",
                        f"step {position}: no step titled {ref!r} to depend on",
                    )
                resolved.append(matches[0])
            else:
                raise TodoError(
                    "validation_error",
                    f"step {position}: dependencies must be indexes or titles",
                )
        conn.execute(
            "UPDATE plan_steps SET dependencies_json = ?, updated_at = ? WHERE id = ?",
            (_json(resolved), now, step_ids[position]),
        )


def create_plan(
    goal: str,
    success_criteria: Any,
    constraints: Any = None,
    steps: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    budget: Any = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Create a plan (Todo List) with optional initial steps."""
    goal = _require_text(goal, "goal")
    criteria = _normalize_criteria(success_criteria, "success_criteria")
    if not criteria:
        raise TodoError("validation_error", "success_criteria must not be empty")
    if steps is not None and not isinstance(steps, list):
        raise TodoError("validation_error", "steps must be a list of step objects")

    conn = _connect()
    try:
        project_id, conversation_id, run_id = _scope_defaults(conn, project_id, conversation_id)
        plan_id = _new_id("plan")
        now = _now()
        status = "ready" if steps else "draft"
        conn.execute(
            "INSERT INTO plans (id, run_id, conversation_id, project_id, goal, mode, status,"
            " success_criteria_json, constraints_json, budget_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, run_id, conversation_id, project_id, goal, mode, status,
                _json(criteria), _json(constraints), _json(budget), now, now,
            ),
        )
        _record_event(conn, plan_id, None, "plan_created", {"goal": goal, "mode": mode})
        step_ids: list[str] = []
        for position, spec in enumerate(steps or []):
            if not isinstance(spec, dict):
                raise TodoError("validation_error", "each step must be an object")
            step_ids.append(_insert_step(conn, plan_id, position, spec))
        if steps:
            _resolve_create_dependencies(conn, plan_id, step_ids, steps)
            _recompute_readiness(conn, plan_id)
        conn.commit()
        return _ok(
            f"plan {plan_id} created ({status}) with {len(step_ids)} step(s)",
            {
                "plan_id": plan_id,
                "status": status,
                "step_ids": step_ids,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "run_id": run_id,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_plan(plan_id: str) -> dict[str, Any]:
    """Fetch a plan with its steps and a progress summary."""
    plan_id = _require_text(plan_id, "plan_id")
    conn = _connect()
    try:
        plan = _fetch_plan(conn, plan_id)
        steps = _fetch_steps(conn, plan_id)
        data = _plan_to_dict(plan)
        data["steps"] = [_step_to_dict(step) for step in steps]
        data["progress"] = _progress(steps)
        return _ok(
            f"plan {plan_id}: {data['status']}, "
            f"{data['progress']['percent_complete']}% complete",
            data,
        )
    finally:
        conn.close()


def list_plans(
    project_id: str | None = None,
    conversation_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List plans, newest first, with keyset pagination."""
    if status is not None and status not in PLAN_STATUSES:
        raise TodoError("validation_error", f"status must be one of {PLAN_STATUSES}")
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise TodoError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > 100:
        raise TodoError("validation_error", "limit must be between 1 and 100")

    conn = _connect()
    try:
        _, conversation_id, _ = _scope_defaults(conn, None, conversation_id)
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if cursor:
            try:
                cursor_created, cursor_id = cursor.split("|", 1)
            except ValueError as exc:
                raise TodoError("validation_error", "malformed cursor") from exc
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created, cursor_created, cursor_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM plans {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        plans = [_plan_to_dict(row) for row in rows]
        next_cursor = None
        if len(rows) == limit:
            last = rows[-1]
            next_cursor = f"{last['created_at']}|{last['id']}"
        return _ok(
            f"{len(plans)} plan(s)",
            {"plans": plans, "count": len(plans), "next_cursor": next_cursor},
        )
    finally:
        conn.close()


def add_step(
    plan_id: str,
    title: str,
    objective: str | None = None,
    dependencies: list[str] | None = None,
    required_capabilities: Any = None,
    success_criteria: Any = None,
    maximum_attempts: int = 2,
) -> dict[str, Any]:
    """Append a step to a non-terminal plan."""
    plan_id = _require_text(plan_id, "plan_id")
    conn = _connect()
    try:
        plan = _fetch_plan(conn, plan_id)
        if plan["status"] in ("completed", "cancelled"):
            raise TodoError(
                "invalid_state", f"cannot add steps to a {plan['status']} plan"
            )
        deps = dependencies or []
        if not isinstance(deps, list):
            raise TodoError("validation_error", "dependencies must be a list of step ids")
        existing_rows = _fetch_steps(conn, plan_id)
        existing = {row["id"] for row in existing_rows}
        unknown = [d for d in deps if d not in existing]
        if unknown:
            raise TodoError("validation_error", f"unknown dependency step ids: {unknown}")
        position = max((row["position"] for row in existing_rows), default=-1) + 1
        step_id = _insert_step(
            conn,
            plan_id,
            position,
            {
                "title": title,
                "objective": objective,
                "dependencies": deps,
                "required_capabilities": required_capabilities,
                "success_criteria": success_criteria,
                "maximum_attempts": maximum_attempts,
            },
        )
        _recompute_readiness(conn, plan_id)
        conn.commit()
        return _ok(
            f"step {step_id} added at position {position}",
            {"plan_id": plan_id, "step_id": step_id, "position": position},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_step(
    plan_id: str,
    step_id: str,
    status: str | None = None,
    objective: str | None = None,
    dependencies: list[str] | None = None,
    required_capabilities: Any = None,
    success_criteria: Any = None,
    result_summary: str | None = None,
    evidence_refs: Any = None,
    failure: Any = None,
) -> dict[str, Any]:
    """Update a step's status and/or fields, enforcing sane transitions."""
    plan_id = _require_text(plan_id, "plan_id")
    step_id = _require_text(step_id, "step_id")
    conn = _connect()
    try:
        plan = _fetch_plan(conn, plan_id)
        if plan["status"] in ("completed", "cancelled"):
            raise TodoError("invalid_state", f"plan is {plan['status']}; steps are frozen")
        steps = _fetch_steps(conn, plan_id)
        step = next((s for s in steps if s["id"] == step_id), None)
        if step is None:
            raise TodoError("not_found", f"step not found in plan: {step_id}")

        now = _now()
        new_status = status
        if new_status is not None:
            if new_status not in STEP_STATUSES:
                raise TodoError(
                    "validation_error", f"status must be one of {STEP_STATUSES}"
                )
            allowed = _STEP_TRANSITIONS[step["status"]]
            if new_status != step["status"] and new_status not in allowed:
                raise TodoError(
                    "invalid_state",
                    f"cannot move step from {step['status']} to {new_status}",
                )

        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if objective is not None:
            sets.append("objective = ?")
            params.append(objective)
        if dependencies is not None:
            if not isinstance(dependencies, list):
                raise TodoError("validation_error", "dependencies must be a list of step ids")
            known = {s["id"] for s in steps}
            unknown = [d for d in dependencies if d not in known]
            if unknown:
                raise TodoError("validation_error", f"unknown dependency step ids: {unknown}")
            if step_id in dependencies:
                raise TodoError("validation_error", "a step cannot depend on itself")
            sets.append("dependencies_json = ?")
            params.append(_json(dependencies))
        if required_capabilities is not None:
            sets.append("required_capabilities_json = ?")
            params.append(_json(_normalize_criteria(required_capabilities, "required_capabilities")))
        if success_criteria is not None:
            sets.append("success_criteria_json = ?")
            params.append(_json(_normalize_criteria(success_criteria, "success_criteria")))
        if result_summary is not None:
            sets.append("result_summary = ?")
            params.append(result_summary)
        if evidence_refs is not None:
            sets.append("evidence_refs_json = ?")
            params.append(_json(evidence_refs if isinstance(evidence_refs, list) else [evidence_refs]))
        if failure is not None:
            sets.append("failure_json = ?")
            params.append(_json(failure))

        final_status = step["status"]
        if new_status is not None and new_status != step["status"]:
            if new_status == "failed":
                attempts = step["attempts"] + 1
                sets.append("attempts = ?")
                params.append(attempts)
                if attempts >= step["maximum_attempts"]:
                    # Retries exhausted: the step is stuck, not just failed.
                    final_status = "blocked"
                    sets.append("failure_json = ?")
                    params.append(_json(failure or {"reason": "maximum attempts reached"}))
                else:
                    final_status = "failed"
            else:
                final_status = new_status
            if final_status == "in_progress" and step["started_at"] is None:
                sets.append("started_at = ?")
                params.append(now)
            if final_status in TERMINAL_STEP_STATUSES:
                sets.append("completed_at = ?")
                params.append(now)
            sets.append("status = ?")
            params.append(final_status)

        params.append(step_id)
        conn.execute(f"UPDATE plan_steps SET {', '.join(sets)} WHERE id = ?", params)
        if final_status != step["status"]:
            _record_event(
                conn, plan_id, step_id, "step_status",
                {"from": step["status"], "to": final_status},
            )
        _recompute_readiness(conn, plan_id)
        plan_status = _recompute_plan_status(conn, plan_id)
        conn.commit()
        return _ok(
            f"step {step_id} now {final_status}; plan {plan_status}",
            {
                "plan_id": plan_id,
                "step_id": step_id,
                "step_status": final_status,
                "plan_status": plan_status,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revise_plan(
    plan_id: str,
    reason: str,
    steps_to_add: list[dict[str, Any]] | None = None,
    steps_to_update: list[dict[str, Any]] | None = None,
    steps_to_remove: list[str] | None = None,
    dependency_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revise a non-completed plan, logging the reason for the revision."""
    plan_id = _require_text(plan_id, "plan_id")
    reason = _require_text(reason, "reason")
    conn = _connect()
    try:
        plan = _fetch_plan(conn, plan_id)
        if plan["status"] in ("completed", "cancelled"):
            raise TodoError("invalid_state", f"cannot revise a {plan['status']} plan")
        now = _now()
        changed: dict[str, list[str]] = {"added": [], "updated": [], "removed": []}

        for spec in steps_to_add or []:
            if not isinstance(spec, dict):
                raise TodoError("validation_error", "steps_to_add entries must be objects")
            rows = _fetch_steps(conn, plan_id)
            position = max((row["position"] for row in rows), default=-1) + 1
            changed["added"].append(_insert_step(conn, plan_id, position, spec))

        for update in steps_to_update or []:
            if not isinstance(update, dict) or not update.get("step_id"):
                raise TodoError(
                    "validation_error",
                    "steps_to_update entries need a step_id plus fields to change",
                )
            target = update["step_id"]
            row = conn.execute(
                "SELECT id FROM plan_steps WHERE id = ? AND plan_id = ?",
                (target, plan_id),
            ).fetchone()
            if row is None:
                raise TodoError("not_found", f"step not found in plan: {target}")
            sets = ["updated_at = ?"]
            params: list[Any] = [now]
            for field, column in (("title", "title"), ("objective", "objective"), ("status", "status")):
                if field in update:
                    if field == "status" and update[field] not in STEP_STATUSES:
                        raise TodoError("validation_error", f"invalid step status: {update[field]}")
                    sets.append(f"{column} = ?")
                    params.append(update[field])
            if "maximum_attempts" in update:
                sets.append("maximum_attempts = ?")
                params.append(int(update["maximum_attempts"]))
            for field, column in (
                ("dependencies", "dependencies_json"),
                ("required_capabilities", "required_capabilities_json"),
                ("success_criteria", "success_criteria_json"),
                ("evidence_refs", "evidence_refs_json"),
                ("failure", "failure_json"),
            ):
                if field in update:
                    sets.append(f"{column} = ?")
                    params.append(_json(update[field]))
            if "result_summary" in update:
                sets.append("result_summary = ?")
                params.append(update["result_summary"])
            params.append(target)
            conn.execute(f"UPDATE plan_steps SET {', '.join(sets)} WHERE id = ?", params)
            changed["updated"].append(target)

        for target in steps_to_remove or []:
            row = conn.execute(
                "SELECT status FROM plan_steps WHERE id = ? AND plan_id = ?",
                (target, plan_id),
            ).fetchone()
            if row is None:
                raise TodoError("not_found", f"step not found in plan: {target}")
            conn.execute(
                "UPDATE plan_steps SET status = 'cancelled', completed_at = ?, updated_at = ?"
                " WHERE id = ?",
                (now, now, target),
            )
            _record_event(conn, plan_id, target, "step_status", {"from": row["status"], "to": "cancelled"})
            changed["removed"].append(target)

        for change in dependency_changes or []:
            if not isinstance(change, dict) or not change.get("step_id"):
                raise TodoError(
                    "validation_error",
                    "dependency_changes entries need step_id and dependencies",
                )
            target = change["step_id"]
            deps = change.get("dependencies") or []
            known = {row["id"] for row in _fetch_steps(conn, plan_id)}
            if target not in known:
                raise TodoError("not_found", f"step not found in plan: {target}")
            unknown = [d for d in deps if d not in known]
            if unknown:
                raise TodoError("validation_error", f"unknown dependency step ids: {unknown}")
            conn.execute(
                "UPDATE plan_steps SET dependencies_json = ?, updated_at = ? WHERE id = ?",
                (_json(deps), now, target),
            )
            changed["updated"].append(target)

        conn.execute("UPDATE plans SET updated_at = ? WHERE id = ?", (now, plan_id))
        _record_event(
            conn, plan_id, None, "plan_revised",
            {"reason": reason, "changed": changed},
        )
        _recompute_readiness(conn, plan_id)
        plan_status = _recompute_plan_status(conn, plan_id)
        conn.commit()
        return _ok(
            f"plan {plan_id} revised: {reason}",
            {"plan_id": plan_id, "changed": changed, "plan_status": plan_status},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_plan_status(plan_id: str, target: str, event_type: str) -> dict[str, Any]:
    """Shared pause/resume/cancel implementation."""
    plan_id = _require_text(plan_id, "plan_id")
    conn = _connect()
    try:
        plan = _fetch_plan(conn, plan_id)
        current = plan["status"]
        allowed = {
            "paused": {"draft", "ready", "running", "blocked"},
            "ready": {"paused"},  # resume target when nothing is mid-flight
            "cancelled": {"draft", "ready", "running", "paused", "blocked", "failed"},
        }
        if target == "ready" and current == "paused":
            # Resume: running again if a step is mid-flight, else ready.
            steps = _fetch_steps(conn, plan_id)
            target = "running" if any(s["status"] == "in_progress" for s in steps) else "ready"
        if current not in allowed.get(target, set()):
            raise TodoError("invalid_state", f"cannot move plan from {current} toward {target}")
        now = _now()
        conn.execute(
            "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
            (target, now, plan_id),
        )
        if target == "cancelled":
            conn.execute(
                "UPDATE plan_steps SET status = 'cancelled', completed_at = ?, updated_at = ?"
                " WHERE plan_id = ? AND status NOT IN ('completed', 'skipped', 'cancelled')",
                (now, now, plan_id),
            )
        _record_event(conn, plan_id, None, event_type, {"from": current, "to": target})
        conn.commit()
        return _ok(
            f"plan {plan_id} {current} → {target}",
            {"plan_id": plan_id, "status": target, "previous_status": current},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_plan(
    plan_id: str,
    result_summary: str,
    evidence_refs: Any = None,
) -> dict[str, Any]:
    """Mark a plan completed; every step must be completed or skipped."""
    plan_id = _require_text(plan_id, "plan_id")
    result_summary = _require_text(result_summary, "result_summary")
    conn = _connect()
    try:
        plan = _fetch_plan(conn, plan_id)
        if plan["status"] == "cancelled":
            raise TodoError("invalid_state", "plan is cancelled")
        now = _now()
        if plan["status"] == "completed":
            # The plan auto-completed when its last step finished. Attaching
            # the final summary is still useful — record it idempotently.
            _record_event(
                conn, plan_id, None, "plan_completed",
                {
                    "result_summary": result_summary,
                    "evidence_refs": evidence_refs if isinstance(evidence_refs, list) else ([evidence_refs] if evidence_refs else []),
                },
            )
            conn.commit()
            return _ok(
                f"plan {plan_id} completed (summary recorded)",
                {"plan_id": plan_id, "status": "completed", "result_summary": result_summary},
            )
        steps = _fetch_steps(conn, plan_id)
        remaining = [
            {"id": s["id"], "title": s["title"], "status": s["status"]}
            for s in steps
            if s["status"] not in ("completed", "skipped")
        ]
        if remaining:
            raise TodoError(
                "invalid_state",
                f"{len(remaining)} step(s) not finished: "
                + ", ".join(f"{r['title']} ({r['status']})" for r in remaining),
            )
        conn.execute(
            "UPDATE plans SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, plan_id),
        )
        _record_event(
            conn, plan_id, None, "plan_completed",
            {
                "result_summary": result_summary,
                "evidence_refs": evidence_refs if isinstance(evidence_refs, list) else ([evidence_refs] if evidence_refs else []),
            },
        )
        conn.commit()
        return _ok(
            f"plan {plan_id} completed",
            {"plan_id": plan_id, "status": "completed", "result_summary": result_summary},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def next_ready_steps(plan_id: str, limit: int = 3) -> dict[str, Any]:
    """Return the dependency-ready steps to work on next."""
    plan_id = _require_text(plan_id, "plan_id")
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise TodoError("validation_error", "limit must be an integer") from exc
    if limit < 1:
        raise TodoError("validation_error", "limit must be >= 1")
    conn = _connect()
    try:
        _fetch_plan(conn, plan_id)
        _recompute_readiness(conn, plan_id)
        conn.commit()
        steps = _fetch_steps(conn, plan_id)
        ready = [_step_to_dict(s) for s in steps if s["status"] == "ready"][:limit]
        in_progress = [_step_to_dict(s) for s in steps if s["status"] == "in_progress"]
        return _ok(
            f"{len(ready)} ready step(s), {len(in_progress)} in progress",
            {
                "plan_id": plan_id,
                "ready": ready,
                "in_progress": in_progress,
                "progress": _progress(steps),
            },
        )
    finally:
        conn.close()


# ── Strands tool wrappers ─────────────────────────────────────────────────


def _wrap(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except TodoError as exc:
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
def todo_create(
    goal: str,
    success_criteria: list[str],
    constraints: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    budget: dict[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Create a Todo List plan that breaks a goal into verifiable steps.

    Use this at the start of any non-trivial task: decompose the goal into
    steps with clear success criteria, then keep the plan updated with
    `todo_update_step` as you work. The plan persists in the app database,
    so it survives across messages in the conversation.

    Args:
        goal: What the plan must achieve, in one sentence.
        success_criteria: How we know the goal is met (list of checkable statements).
        constraints: Optional limits the work must respect (time, scope, policy).
        steps: Optional initial steps, each {title, objective, dependencies?,
            required_capabilities?, success_criteria?, maximum_attempts?}.
            `dependencies` reference other steps in this list by 0-based index
            or by title; use `todo_add_step` later to depend on existing ids.
        project_id: Optional project scope (defaults from the conversation).
        conversation_id: Optional conversation scope (defaults from the run context).
        budget: Optional budget object, e.g. {"maximum_iterations": 10}.
        mode: Optional execution mode label (e.g. "research", "build").

    Returns:
        `{ok, summary, data: {plan_id, status, step_ids, ...}, error}`.
    """
    return _wrap(
        create_plan, goal, success_criteria, constraints, steps,
        project_id, conversation_id, budget, mode,
    )


@tool
def todo_get(plan_id: str) -> dict[str, Any]:
    """Fetch a Todo List plan with all steps and a progress summary.

    Args:
        plan_id: The plan id returned by todo_create.

    Returns:
        `{ok, summary, data: {plan fields..., steps: [...], progress: {...}}, error}`.
    """
    return _wrap(get_plan, plan_id)


@tool
def todo_list(
    project_id: str | None = None,
    conversation_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List Todo List plans, newest first.

    Args:
        project_id: Optional project filter.
        conversation_id: Optional conversation filter (defaults to the current conversation).
        status: Optional filter: draft, ready, running, paused, blocked, failed, completed, cancelled.
        limit: Page size, 1-100 (default 20).
        cursor: Pagination cursor from a previous response's next_cursor.

    Returns:
        `{ok, summary, data: {plans: [...], count, next_cursor}, error}`.
    """
    return _wrap(list_plans, project_id, conversation_id, status, limit, cursor)


@tool
def todo_add_step(
    plan_id: str,
    title: str,
    objective: str | None = None,
    dependencies: list[str] | None = None,
    required_capabilities: list[str] | None = None,
    success_criteria: list[str] | None = None,
    maximum_attempts: int = 2,
) -> dict[str, Any]:
    """Append a step to an existing Todo List plan.

    Args:
        plan_id: Target plan.
        title: Short step name.
        objective: What this step must accomplish.
        dependencies: Existing step ids that must complete first.
        required_capabilities: Capabilities/tools this step needs.
        success_criteria: How we verify this step's result.
        maximum_attempts: Failures tolerated before the step is blocked (default 2).

    Returns:
        `{ok, summary, data: {plan_id, step_id, position}, error}`.
    """
    return _wrap(
        add_step, plan_id, title, objective, dependencies,
        required_capabilities, success_criteria, maximum_attempts,
    )


@tool
def todo_update_step(
    plan_id: str,
    step_id: str,
    status: str | None = None,
    objective: str | None = None,
    dependencies: list[str] | None = None,
    required_capabilities: list[str] | None = None,
    success_criteria: list[str] | None = None,
    result_summary: str | None = None,
    evidence_refs: list[str] | None = None,
    failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update a step as work progresses — keep the plan current.

    Mark steps in_progress when you start them, completed (with
    result_summary and evidence_refs) when done, or failed when an attempt
    did not work. A failed step consumes one attempt; when attempts reach
    maximum_attempts the step is automatically blocked. Completing steps
    unblocks their dependents, and finishing every step completes the plan.

    Args:
        plan_id: Plan containing the step.
        step_id: Step to update.
        status: New status (in_progress, completed, failed, skipped, ...).
        objective: Revised objective text.
        dependencies: Replacement dependency list (step ids).
        required_capabilities: Replacement capability list.
        success_criteria: Replacement criteria list.
        result_summary: What the step produced (set on completion).
        evidence_refs: References backing the result (paths, urls, ids).
        failure: Failure detail object (set on failure).

    Returns:
        `{ok, summary, data: {step_status, plan_status, ...}, error}`.
    """
    return _wrap(
        update_step, plan_id, step_id, status, objective, dependencies,
        required_capabilities, success_criteria, result_summary, evidence_refs, failure,
    )


@tool
def todo_revise(
    plan_id: str,
    reason: str,
    steps_to_add: list[dict[str, Any]] | None = None,
    steps_to_update: list[dict[str, Any]] | None = None,
    steps_to_remove: list[str] | None = None,
    dependency_changes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Revise a plan that is still in flight, with an explicit reason.

    Use when the approach changes: add newly discovered steps, update or
    cancel obsolete ones, rewire dependencies. The reason is recorded in
    the plan's event log. Completed or cancelled plans cannot be revised.

    Args:
        plan_id: Plan to revise.
        reason: Why the plan is changing (recorded in the event log).
        steps_to_add: New step objects (same shape as todo_create steps;
            dependencies here are existing step ids).
        steps_to_update: [{step_id, title?, objective?, status?, ...}] patches.
        steps_to_remove: Step ids to cancel.
        dependency_changes: [{step_id, dependencies: [step ids]}] rewiring.

    Returns:
        `{ok, summary, data: {changed: {added, updated, removed}, plan_status}, error}`.
    """
    return _wrap(
        revise_plan, plan_id, reason, steps_to_add,
        steps_to_update, steps_to_remove, dependency_changes,
    )


@tool
def todo_pause(plan_id: str) -> dict[str, Any]:
    """Pause an active plan (e.g. waiting on the user or an external event).

    Args:
        plan_id: Plan to pause.

    Returns:
        `{ok, summary, data: {plan_id, status, previous_status}, error}`.
    """
    return _wrap(_set_plan_status, plan_id, "paused", "plan_paused")


@tool
def todo_resume(plan_id: str) -> dict[str, Any]:
    """Resume a paused plan.

    Args:
        plan_id: Plan to resume.

    Returns:
        `{ok, summary, data: {plan_id, status, previous_status}, error}`.
    """
    return _wrap(_set_plan_status, plan_id, "ready", "plan_resumed")


@tool
def todo_cancel(plan_id: str) -> dict[str, Any]:
    """Cancel a plan and all its unfinished steps.

    Args:
        plan_id: Plan to cancel.

    Returns:
        `{ok, summary, data: {plan_id, status, previous_status}, error}`.
    """
    return _wrap(_set_plan_status, plan_id, "cancelled", "plan_cancelled")


@tool
def todo_complete(
    plan_id: str,
    result_summary: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Mark a plan completed with a final summary.

    Only succeeds when every step is completed or skipped; otherwise the
    response lists what remains. Record the outcome so future runs can see
    what the plan achieved.

    Args:
        plan_id: Plan to complete.
        result_summary: What the plan achieved overall.
        evidence_refs: References backing the outcome (paths, urls, ids).

    Returns:
        `{ok, summary, data: {plan_id, status, result_summary}, error}`.
    """
    return _wrap(complete_plan, plan_id, result_summary, evidence_refs)


@tool
def todo_next_ready_steps(plan_id: str, limit: int = 3) -> dict[str, Any]:
    """Show which steps can be worked on next, honoring dependencies.

    Args:
        plan_id: Plan to inspect.
        limit: Maximum ready steps to return (default 3).

    Returns:
        `{ok, summary, data: {ready: [...], in_progress: [...], progress: {...}}, error}`.
    """
    return _wrap(next_ready_steps, plan_id, limit)


TOOL = [
    todo_create,
    todo_get,
    todo_list,
    todo_add_step,
    todo_update_step,
    todo_revise,
    todo_pause,
    todo_resume,
    todo_cancel,
    todo_complete,
    todo_next_ready_steps,
]
