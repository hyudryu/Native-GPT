"""Goal Supervisor Strands tools — goal contracts + deterministic validation.

Multi-tool folder: `TOOL` is a list of Strands tools. State lives in the app
database (`goal_contracts`, `goal_validation_results`,
`run_recovery_attempts` — migration 0011), opened through
`tools/_lib/db.py`; run/conversation ids default from
`tools/_lib/context.py`. Both `_lib` modules are loaded by file path
because the runtime imports each tool.py as a standalone module.

Evidence contract
-----------------
Some success criteria can be checked entirely against local state (the
database, the filesystem). Criteria about the run's stream — what the
answer said, which tools fired — are invisible to this tool, so the agent
supplies them in the `evidence` dict passed to `goal_evaluate`:

- ``answer_text`` (str): the candidate final answer.
- ``tool_calls`` (list): ``[{"name": "web_search", "ok": true}, ...]``.
- ``sources`` (list): evidence/source refs; items may be dicts with
  ``ok``/``status`` to mark retrieval success.
- ``artifact_id`` (str): id of an artifacts-table row to check.
- ``json_text`` (str) or ``json_data``: candidate structured output for
  ``json_schema_valid``.
- ``plan_id`` (str): Todo List plan for ``plan_steps_completed``.

Missing evidence makes the corresponding criterion fail with detail
``missing_evidence`` — never silently pass.
"""

from __future__ import annotations

import hashlib
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
_paths = _load_lib("paths.py", "agentgpt_tools_paths")

CONTRACT_STATUSES = ("active", "completed", "blocked", "cancelled")
DEFAULT_MAX_RECOVERY_ATTEMPTS = 3
PROGRESS_HISTORY_LIMIT = 50


class GoalError(ValueError):
    """Any goal-supervisor failure; `code` becomes the result error code."""

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
        raise GoalError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise GoalError("db_unavailable", str(exc)) from exc


def _fetch_contract(conn: sqlite3.Connection, contract_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM goal_contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    if row is None:
        raise GoalError("not_found", f"goal contract not found: {contract_id}")
    return row


def _contract_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "conversation_id": row["conversation_id"],
        "goal": row["goal"],
        "task_type": row["task_type"],
        "success_criteria": _parse_json(row["success_criteria_json"], []),
        "required_capabilities": _parse_json(row["required_capabilities_json"], []),
        "budgets": _parse_json(row["budgets_json"], {}),
        "progress": _parse_json(row["progress_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "blocked_reason": row["blocked_reason"],
    }


def _evidence_sources(evidence: dict[str, Any]) -> list[Any]:
    sources = evidence.get("sources")
    if isinstance(sources, list):
        return sources
    refs = evidence.get("evidence_refs")
    if isinstance(refs, list):
        return refs
    return []


def _source_successful(item: Any) -> bool:
    """Unannotated sources count as successful; explicit flags win."""
    if isinstance(item, dict):
        if "ok" in item:
            return bool(item["ok"])
        status = item.get("status")
        if isinstance(status, str):
            return status.lower() in ("ok", "success", "successful", "completed")
    return True


def _check_json_schema(candidate: Any, schema: dict[str, Any]) -> list[str]:
    """Minimal JSON-schema subset: type, required, one level of properties."""
    problems: list[str] = []
    type_map = {
        "object": dict, "array": list, "string": str,
        "number": (int, float), "integer": int, "boolean": bool,
    }
    expected = schema.get("type")
    if expected in type_map and not isinstance(candidate, type_map[expected]):
        # bool is a subclass of int; exclude it for number/integer checks.
        if not (expected in ("number", "integer") and isinstance(candidate, bool)):
            problems.append(f"expected type {expected}")
            return problems
    if isinstance(candidate, dict):
        for key in schema.get("required", []):
            if key not in candidate:
                problems.append(f"missing required key {key!r}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key in candidate and isinstance(subschema, dict):
                    problems.extend(
                        f"{key}: {p}" for p in _check_json_schema(candidate[key], subschema)
                    )
    return problems


def _run_validator(
    conn: sqlite3.Connection,
    contract: sqlite3.Row,
    spec: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Evaluate one success criterion; returns (passed, detail)."""
    vtype = spec.get("type")
    detail: dict[str, Any] = {"type": vtype}

    if vtype == "answer_nonempty":
        answer = evidence.get("answer_text", evidence.get("answer"))
        if not isinstance(answer, str) or not answer.strip():
            detail["reason"] = "missing_evidence" if answer is None else "empty_answer"
            return False, detail
        detail["length"] = len(answer.strip())
        return True, detail

    if vtype == "minimum_response_characters":
        threshold = int(spec.get("value", 1))
        answer = evidence.get("answer_text", evidence.get("answer"))
        if not isinstance(answer, str):
            detail["reason"] = "missing_evidence"
            return False, detail
        detail.update({"length": len(answer), "required": threshold})
        return len(answer) >= threshold, detail

    if vtype == "required_tool_called":
        wanted = spec.get("tool") or spec.get("name")
        calls = evidence.get("tool_calls")
        if not isinstance(calls, list):
            detail["reason"] = "missing_evidence"
            return False, detail
        names = [c.get("name") for c in calls if isinstance(c, dict)]
        detail.update({"tool": wanted, "called": names})
        return wanted in names, detail

    if vtype == "minimum_successful_tool_calls":
        threshold = int(spec.get("value", 1))
        calls = evidence.get("tool_calls")
        if not isinstance(calls, list):
            detail["reason"] = "missing_evidence"
            return False, detail
        successes = sum(1 for c in calls if isinstance(c, dict) and c.get("ok"))
        detail.update({"successful": successes, "required": threshold})
        return successes >= threshold, detail

    if vtype == "artifact_exists":
        artifact_id = spec.get("artifact_id") or evidence.get("artifact_id")
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if artifact_id:
            clauses.append("id = ?")
            params.append(artifact_id)
        if spec.get("name"):
            clauses.append("name = ?")
            params.append(spec["name"])
        if spec.get("mime_type"):
            clauses.append("mime_type = ?")
            params.append(spec["mime_type"])
        if not artifact_id and not spec.get("name") and not spec.get("mime_type"):
            detail["reason"] = "missing_evidence"
            return False, detail
        # Prefer artifacts from this contract's conversation when no explicit
        # id narrows the query.
        if not artifact_id and contract["conversation_id"]:
            clauses.append("conversation_id = ?")
            params.append(contract["conversation_id"])
        row = conn.execute(
            f"SELECT id, name, mime_type FROM artifacts WHERE {' AND '.join(clauses)} LIMIT 1",
            params,
        ).fetchone()
        if row:
            detail.update({"artifact_id": row["id"], "name": row["name"]})
            return True, detail
        detail["reason"] = "no_matching_artifact"
        return False, detail

    if vtype == "file_exists":
        path = spec.get("path")
        if not path:
            detail["reason"] = "missing_path"
            return False, detail
        try:
            resolved = _paths.resolve_under_root(path)
        except _paths.PathEscapeError as exc:
            detail["reason"] = str(exc)
            return False, detail
        detail["path"] = str(resolved)
        return resolved.is_file(), detail

    if vtype == "file_hash_changed":
        path = spec.get("path")
        previous = spec.get("previous_sha256")
        if not path or not previous:
            detail["reason"] = "missing_path_or_hash"
            return False, detail
        try:
            resolved = _paths.resolve_under_root(path)
        except _paths.PathEscapeError as exc:
            detail["reason"] = str(exc)
            return False, detail
        if not resolved.is_file():
            detail["reason"] = "file_missing"
            return False, detail
        current = hashlib.sha256(resolved.read_bytes()).hexdigest()
        detail.update({"path": str(resolved), "current_sha256": current})
        return current != previous, detail

    if vtype == "json_schema_valid":
        candidate = evidence.get("json_data")
        if candidate is None:
            raw = evidence.get("json_text")
            if not isinstance(raw, str):
                detail["reason"] = "missing_evidence"
                return False, detail
            try:
                candidate = json.loads(raw)
            except json.JSONDecodeError as exc:
                detail["reason"] = f"invalid JSON: {exc}"
                return False, detail
        problems: list[str] = []
        for key in spec.get("required_keys", []):
            if not isinstance(candidate, dict) or key not in candidate:
                problems.append(f"missing required key {key!r}")
        schema = spec.get("schema")
        if isinstance(schema, dict):
            problems.extend(_check_json_schema(candidate, schema))
        if not spec.get("required_keys") and not isinstance(schema, dict):
            pass  # parse-ability alone was the criterion
        detail["problems"] = problems
        return not problems, detail

    if vtype == "plan_steps_completed":
        plan_id = spec.get("plan_id") or evidence.get("plan_id")
        if not plan_id:
            detail["reason"] = "missing_evidence"
            return False, detail
        rows = conn.execute(
            "SELECT status FROM plan_steps WHERE plan_id = ?", (plan_id,)
        ).fetchall()
        if not rows:
            detail["reason"] = "plan_not_found_or_empty"
            return False, detail
        minimum = spec.get("minimum_completed")
        done = sum(1 for r in rows if r["status"] in ("completed", "skipped"))
        detail.update({"plan_id": plan_id, "completed": done, "total": len(rows)})
        if minimum is not None:
            detail["required"] = int(minimum)
            return done >= int(minimum), detail
        unfinished = [r["status"] for r in rows if r["status"] not in ("completed", "skipped")]
        detail["unfinished_statuses"] = unfinished
        return not unfinished, detail

    if vtype in ("minimum_sources", "minimum_successful_sources"):
        threshold = int(spec.get("value", 1))
        sources = _evidence_sources(evidence)
        if not sources and "sources" not in evidence and "evidence_refs" not in evidence:
            detail["reason"] = "missing_evidence"
            return False, detail
        if vtype == "minimum_successful_sources":
            count = sum(1 for item in sources if _source_successful(item))
        else:
            count = len(sources)
        detail.update({"count": count, "required": threshold})
        return count >= threshold, detail

    if vtype == "knowledge_source_exists":
        clauses = ["deleted_at IS NULL", "enabled = 1"]
        params = []
        if spec.get("source_id"):
            clauses.append("id = ?")
            params.append(spec["source_id"])
        elif spec.get("title"):
            clauses.append("title = ?")
            params.append(spec["title"])
        else:
            detail["reason"] = "missing_source_id_or_title"
            return False, detail
        row = conn.execute(
            f"SELECT id, title FROM knowledge_sources WHERE {' AND '.join(clauses)} LIMIT 1",
            params,
        ).fetchone()
        if row:
            detail.update({"source_id": row["id"], "title": row["title"]})
            return True, detail
        detail["reason"] = "no_matching_knowledge_source"
        return False, detail

    if vtype == "memory_record_exists":
        key = spec.get("memory_key")
        if not key:
            detail["reason"] = "missing_memory_key"
            return False, detail
        clauses = ["memory_key = ?", "deleted_at IS NULL"]
        params = [key]
        if spec.get("scope"):
            clauses.append("scope = ?")
            params.append(spec["scope"])
        if spec.get("scope_id"):
            clauses.append("scope_id = ?")
            params.append(spec["scope_id"])
        row = conn.execute(
            f"SELECT id FROM memories WHERE {' AND '.join(clauses)} LIMIT 1", params
        ).fetchone()
        detail["memory_key"] = key
        if row:
            detail["memory_id"] = row["id"]
            return True, detail
        detail["reason"] = "no_matching_memory"
        return False, detail

    detail["reason"] = "unknown_validator"
    return False, detail


def create_contract(
    goal: str,
    task_type: str,
    success_criteria: list[dict[str, Any]],
    required_capabilities: list[str] | None = None,
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a goal contract with validator specs and budgets."""
    goal = _require_text(goal, "goal")
    task_type = _require_text(task_type, "task_type")
    if not isinstance(success_criteria, list) or not success_criteria:
        raise GoalError("validation_error", "success_criteria must be a non-empty list")
    for spec in success_criteria:
        if not isinstance(spec, dict) or not spec.get("type"):
            raise GoalError(
                "validation_error", "each success criterion needs a 'type' field"
            )
    if budgets is not None and not isinstance(budgets, dict):
        raise GoalError("validation_error", "budgets must be an object")

    conn = _connect()
    try:
        ctx = _context.get_run_context()
        contract_id = _new_id("goal")
        now = _now()
        conn.execute(
            "INSERT INTO goal_contracts (id, run_id, conversation_id, goal, task_type,"
            " success_criteria_json, required_capabilities_json, budgets_json, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                contract_id,
                ctx.get("run_id"),
                ctx.get("conversation_id"),
                goal,
                task_type,
                _json(success_criteria),
                _json(required_capabilities or []),
                _json(budgets or {}),
                now,
                now,
            ),
        )
        conn.commit()
        return _ok(
            f"goal contract {contract_id} created ({len(success_criteria)} criteria)",
            {
                "contract_id": contract_id,
                "status": "active",
                "run_id": ctx.get("run_id"),
                "conversation_id": ctx.get("conversation_id"),
                "criteria_count": len(success_criteria),
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_status(contract_id: str) -> dict[str, Any]:
    """Contract plus its latest validation result per criterion."""
    contract_id = _require_text(contract_id, "contract_id")
    conn = _connect()
    try:
        contract = _fetch_contract(conn, contract_id)
        rows = conn.execute(
            "SELECT * FROM goal_validation_results WHERE contract_id = ?"
            " ORDER BY created_at DESC, id DESC",
            (contract_id,),
        ).fetchall()
        latest_by_index: dict[int, dict[str, Any]] = {}
        for row in rows:
            detail = _parse_json(row["detail_json"], {})
            index = detail.get("index")
            if index is None or index in latest_by_index:
                continue
            latest_by_index[index] = {
                "validator": row["validator"],
                "passed": bool(row["passed"]),
                "detail": detail,
                "created_at": row["created_at"],
            }
        criteria = _parse_json(contract["success_criteria_json"], [])
        validations = [latest_by_index.get(i) for i in range(len(criteria))]
        evaluated = sum(1 for v in validations if v is not None)
        passed = sum(1 for v in validations if v and v["passed"])
        data = _contract_to_dict(contract)
        data["latest_validations"] = validations
        data["validation_summary"] = {
            "criteria": len(criteria),
            "evaluated": evaluated,
            "passing": passed,
        }
        return _ok(
            f"contract {contract_id}: {contract['status']}, "
            f"{passed}/{len(criteria)} criteria passing",
            data,
        )
    finally:
        conn.close()


def record_progress(
    contract_id: str,
    completed_items: list[str] | None = None,
    remaining_items: list[str] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Merge a progress snapshot into the contract's progress_json."""
    contract_id = _require_text(contract_id, "contract_id")
    conn = _connect()
    try:
        contract = _fetch_contract(conn, contract_id)
        if contract["status"] != "active":
            raise GoalError(
                "invalid_state", f"contract is {contract['status']}; progress is frozen"
            )
        progress = _parse_json(contract["progress_json"], {}) or {}
        now = _now()
        for key, value in (
            ("completed_items", completed_items),
            ("remaining_items", remaining_items),
            ("evidence_refs", evidence_refs),
        ):
            if value is None:
                continue
            if not isinstance(value, list):
                raise GoalError("validation_error", f"{key} must be a list")
            progress[key] = value
        history = progress.get("history") or []
        history.append(
            {
                "at": now,
                "completed": len(progress.get("completed_items") or []),
                "remaining": len(progress.get("remaining_items") or []),
            }
        )
        progress["history"] = history[-PROGRESS_HISTORY_LIMIT:]
        progress["updated_at"] = now
        conn.execute(
            "UPDATE goal_contracts SET progress_json = ?, updated_at = ? WHERE id = ?",
            (_json(progress), now, contract_id),
        )
        conn.commit()
        return _ok(
            f"progress recorded: {len(progress.get('completed_items') or [])} done, "
            f"{len(progress.get('remaining_items') or [])} remaining",
            {"contract_id": contract_id, "progress": progress},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def evaluate(contract_id: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run every success criterion and store the results."""
    contract_id = _require_text(contract_id, "contract_id")
    if evidence is not None and not isinstance(evidence, dict):
        raise GoalError("validation_error", "evidence must be an object")
    evidence = evidence or {}
    conn = _connect()
    try:
        contract = _fetch_contract(conn, contract_id)
        if contract["status"] in ("completed", "cancelled"):
            raise GoalError(
                "invalid_state", f"contract is {contract['status']}; cannot evaluate"
            )
        criteria = _parse_json(contract["success_criteria_json"], [])
        results: list[dict[str, Any]] = []
        now = _now()
        for index, spec in enumerate(criteria):
            passed, detail = _run_validator(conn, contract, spec, evidence)
            detail["index"] = index
            conn.execute(
                "INSERT INTO goal_validation_results (id, contract_id, validator, passed,"
                " detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (_new_id("val"), contract_id, spec.get("type"), int(passed), _json(detail), now),
            )
            results.append(
                {"index": index, "validator": spec.get("type"), "passed": passed, "detail": detail}
            )
        conn.execute(
            "UPDATE goal_contracts SET updated_at = ? WHERE id = ?", (now, contract_id)
        )
        conn.commit()
        overall = all(r["passed"] for r in results) if results else False
        failing = [r["validator"] for r in results if not r["passed"]]
        return _ok(
            f"evaluation {'PASSED' if overall else 'FAILED'}: "
            f"{sum(1 for r in results if r['passed'])}/{len(results)} criteria"
            + (f"; failing: {', '.join(str(f) for f in failing)}" if failing else ""),
            {
                "contract_id": contract_id,
                "passed": overall,
                "results": results,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_recovery(contract_id: str, reason: str) -> dict[str, Any]:
    """Record a recovery attempt and return the recovery prompt text."""
    contract_id = _require_text(contract_id, "contract_id")
    reason = _require_text(reason, "reason")
    conn = _connect()
    try:
        contract = _fetch_contract(conn, contract_id)
        if contract["status"] != "active":
            raise GoalError(
                "invalid_state", f"contract is {contract['status']}; recovery not applicable"
            )
        budgets = _parse_json(contract["budgets_json"], {}) or {}
        maximum = int(budgets.get("maximum_recovery_attempts", DEFAULT_MAX_RECOVERY_ATTEMPTS))
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM run_recovery_attempts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()["n"]
        if used >= maximum:
            return {
                "ok": False,
                "summary": (
                    f"recovery budget exhausted ({used}/{maximum}); "
                    "mark the goal blocked with goal_mark_blocked"
                ),
                "data": {
                    "contract_id": contract_id,
                    "attempts_used": used,
                    "maximum_recovery_attempts": maximum,
                    "advice": "goal_mark_blocked",
                },
                "error": {
                    "code": "budget_exhausted",
                    "message": "maximum_recovery_attempts reached",
                },
            }
        attempt_id = _new_id("rec")
        conn.execute(
            "INSERT INTO run_recovery_attempts (id, run_id, contract_id, reason, strategy,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'requested', ?)",
            (
                attempt_id,
                contract["run_id"],
                contract_id,
                reason,
                "retry_with_recovery_prompt",
                _now(),
            ),
        )
        conn.commit()
        progress = _parse_json(contract["progress_json"], {}) or {}
        completed = progress.get("completed_items") or []
        remaining = progress.get("remaining_items") or []
        recovery_prompt = (
            f"GOAL SUPERVISOR — RECOVERY ATTEMPT {used + 1}/{maximum}\n"
            f"Original goal: {contract['goal']}\n"
            f"Failure reason: {reason}\n"
            f"Completed so far: "
            + ("; ".join(completed) if completed else "(nothing recorded — call goal_record_progress)")
            + "\nRemaining: "
            + ("; ".join(remaining) if remaining else "(unknown — reassess the remaining work)")
            + "\nRequired next action: address the failure reason above, then continue "
            "with the remaining items and re-validate with goal_evaluate."
        )
        return _ok(
            f"recovery attempt {used + 1}/{maximum} recorded",
            {
                "contract_id": contract_id,
                "attempt_id": attempt_id,
                "attempts_used": used + 1,
                "maximum_recovery_attempts": maximum,
                "recovery_prompt": recovery_prompt,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_blocked(
    contract_id: str,
    reason: str,
    attempted_recoveries: list[str] | None = None,
) -> dict[str, Any]:
    """Mark a contract blocked with a reason the user can act on."""
    contract_id = _require_text(contract_id, "contract_id")
    reason = _require_text(reason, "reason")
    conn = _connect()
    try:
        contract = _fetch_contract(conn, contract_id)
        if contract["status"] in ("completed", "cancelled"):
            raise GoalError("invalid_state", f"contract is already {contract['status']}")
        now = _now()
        conn.execute(
            "UPDATE goal_contracts SET status = 'blocked', blocked_reason = ?, updated_at = ?"
            " WHERE id = ?",
            (reason, now, contract_id),
        )
        for summary in attempted_recoveries or []:
            conn.execute(
                "INSERT INTO run_recovery_attempts (id, run_id, contract_id, reason,"
                " strategy, status, created_at) VALUES (?, ?, ?, ?, 'manual', 'exhausted', ?)",
                (_new_id("rec"), contract["run_id"], contract_id, summary, now),
            )
        conn.commit()
        return _ok(
            f"contract {contract_id} blocked: {reason}",
            {"contract_id": contract_id, "status": "blocked", "blocked_reason": reason},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_complete(
    contract_id: str,
    validation_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Mark a contract completed; every criterion must have a passing result.

    Completion requires proof: pass `validation_results` (list of
    {index or validator, passed}) from a fresh evaluation, or rely on the
    latest stored result per criterion from `goal_evaluate`.
    """
    contract_id = _require_text(contract_id, "contract_id")
    conn = _connect()
    try:
        contract = _fetch_contract(conn, contract_id)
        if contract["status"] != "active":
            raise GoalError("invalid_state", f"contract is {contract['status']}")
        criteria = _parse_json(contract["success_criteria_json"], [])

        if validation_results is not None:
            if not isinstance(validation_results, list):
                raise GoalError("validation_error", "validation_results must be a list")
            supplied: dict[int, bool] = {}
            for item in validation_results:
                if not isinstance(item, dict):
                    raise GoalError(
                        "validation_error", "validation_results entries must be objects"
                    )
                if "index" in item:
                    supplied[int(item["index"])] = bool(item.get("passed"))
                elif "validator" in item:
                    for i, spec in enumerate(criteria):
                        if spec.get("type") == item["validator"]:
                            supplied[i] = bool(item.get("passed"))
            verdicts = [supplied.get(i) for i in range(len(criteria))]
            source = "supplied"
        else:
            rows = conn.execute(
                "SELECT * FROM goal_validation_results WHERE contract_id = ?"
                " ORDER BY created_at DESC, id DESC",
                (contract_id,),
            ).fetchall()
            latest: dict[int, bool] = {}
            for row in rows:
                detail = _parse_json(row["detail_json"], {})
                index = detail.get("index")
                if index is None or index in latest:
                    continue
                latest[index] = bool(row["passed"])
            verdicts = [latest.get(i) for i in range(len(criteria))]
            source = "stored"

        missing = [i for i, v in enumerate(verdicts) if v is None]
        failing = [i for i, v in enumerate(verdicts) if v is False]
        if missing:
            raise GoalError(
                "invalid_state",
                f"no validation result for criteria {missing}; run goal_evaluate first",
            )
        if failing:
            raise GoalError(
                "invalid_state",
                f"criteria {failing} are not passing; recover or revise the goal",
            )
        now = _now()
        conn.execute(
            "UPDATE goal_contracts SET status = 'completed', completed_at = ?, updated_at = ?"
            " WHERE id = ?",
            (now, now, contract_id),
        )
        conn.commit()
        return _ok(
            f"contract {contract_id} completed ({len(criteria)} criteria verified via {source} results)",
            {
                "contract_id": contract_id,
                "status": "completed",
                "criteria_verified": len(criteria),
                "verification_source": source,
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
    except GoalError as exc:
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
def goal_create_contract(
    goal: str,
    task_type: str,
    success_criteria: list[dict[str, Any]],
    required_capabilities: list[str] | None = None,
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a goal contract for a task before starting substantial work.

    The contract pins down what "done" means as deterministic success
    criteria, so completion is verified — not vibes. Evaluate with
    goal_evaluate, track work with goal_record_progress, and close with
    goal_mark_complete only when every criterion passes.

    Args:
        goal: The task's objective in one sentence.
        task_type: Category label (e.g. "research", "build", "write").
        success_criteria: Validator specs, e.g.
            [{"type": "answer_nonempty"},
             {"type": "minimum_successful_sources", "value": 3},
             {"type": "artifact_exists", "mime_type": "text/markdown"},
             {"type": "file_exists", "path": "report/out.md"},
             {"type": "plan_steps_completed", "plan_id": "plan_..."}].
            Supported types: answer_nonempty, minimum_response_characters,
            required_tool_called, minimum_successful_tool_calls,
            artifact_exists, file_exists, file_hash_changed,
            json_schema_valid, plan_steps_completed, minimum_sources,
            minimum_successful_sources, knowledge_source_exists,
            memory_record_exists.
        required_capabilities: Capabilities the task depends on.
        budgets: Optional {maximum_iterations, maximum_tool_calls,
            maximum_recovery_attempts, maximum_duration_seconds}.

    Returns:
        `{ok, summary, data: {contract_id, status, criteria_count, ...}, error}`.
    """
    return _wrap(create_contract, goal, task_type, success_criteria, required_capabilities, budgets)


@tool
def goal_get_status(contract_id: str) -> dict[str, Any]:
    """Fetch a goal contract with its latest validation results and progress.

    Args:
        contract_id: The contract id from goal_create_contract.

    Returns:
        `{ok, summary, data: {contract fields..., latest_validations, validation_summary}, error}`.
    """
    return _wrap(get_status, contract_id)


@tool
def goal_record_progress(
    contract_id: str,
    completed_items: list[str] | None = None,
    remaining_items: list[str] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Record what is done and what remains against a goal contract.

    Keep this current as you work — it feeds the recovery prompt when the
    run fails, so recovery can resume from accurate state.

    Args:
        contract_id: Target contract.
        completed_items: Replaces the completed-items list.
        remaining_items: Replaces the remaining-items list.
        evidence_refs: Replaces the evidence reference list.

    Returns:
        `{ok, summary, data: {contract_id, progress}, error}`.
    """
    return _wrap(record_progress, contract_id, completed_items, remaining_items, evidence_refs)


@tool
def goal_evaluate(contract_id: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate every success criterion deterministically and store results.

    Criteria about local state (artifacts, files, plans, knowledge sources,
    memories) are checked directly. Criteria about the run's stream need
    the `evidence` dict: answer_text (the candidate answer), tool_calls
    ([{name, ok}]), sources, artifact_id, json_text/json_data, plan_id.
    Criteria whose evidence is missing FAIL with reason missing_evidence.

    Args:
        contract_id: Contract to evaluate.
        evidence: Run-stream evidence described above.

    Returns:
        `{ok, summary, data: {passed, results: [{index, validator, passed, detail}]}, error}`.
    """
    return _wrap(evaluate, contract_id, evidence)


@tool
def goal_request_recovery(contract_id: str, reason: str) -> dict[str, Any]:
    """Request a recovery attempt when the run has gone off track.

    Enforces the contract's maximum_recovery_attempts budget (default 3).
    While budget remains, returns a recovery prompt restating the original
    goal, what is completed, what remains, and the required next action —
    follow it to get back on track. When the budget is exhausted, the
    response advises goal_mark_blocked instead.

    Args:
        contract_id: Contract that needs recovery.
        reason: What failed or went wrong.

    Returns:
        `{ok, summary, data: {recovery_prompt, attempts_used, maximum_recovery_attempts}, error}`.
    """
    return _wrap(request_recovery, contract_id, reason)


@tool
def goal_mark_blocked(
    contract_id: str,
    reason: str,
    attempted_recoveries: list[str] | None = None,
) -> dict[str, Any]:
    """Mark a goal contract blocked when recovery cannot succeed.

    Args:
        contract_id: Contract to block.
        reason: User-actionable explanation of the blocker.
        attempted_recoveries: Optional summaries of recoveries already tried
            (recorded for audit).

    Returns:
        `{ok, summary, data: {contract_id, status, blocked_reason}, error}`.
    """
    return _wrap(mark_blocked, contract_id, reason, attempted_recoveries)


@tool
def goal_mark_complete(
    contract_id: str,
    validation_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Mark a goal contract completed — only when every criterion passes.

    Args:
        contract_id: Contract to complete.
        validation_results: Optional fresh results [{index or validator,
            passed}]; when omitted, the latest stored goal_evaluate result
            per criterion is used. Completion is refused if any criterion is
            failing or has never been evaluated.

    Returns:
        `{ok, summary, data: {contract_id, status, criteria_verified}, error}`.
    """
    return _wrap(mark_complete, contract_id, validation_results)


TOOL = [
    goal_create_contract,
    goal_get_status,
    goal_record_progress,
    goal_evaluate,
    goal_request_recovery,
    goal_mark_blocked,
    goal_mark_complete,
]
