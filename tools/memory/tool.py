"""Scoped Assistant Memory Strands tools — concise reusable state/preferences.

Multi-tool folder: `TOOL` is a list of Strands tools. State lives in the app
database (`memories`, `memories_fts`, `memory_proposals` — migration 0011),
opened through `tools/_lib/db.py`; run/conversation ids default from
`tools/_lib/context.py`. Feature-hash embeddings come from
`tools/_lib/vectorize.py` (a stdlib port of the Rust host's `vectorize`).
All three `_lib` modules are loaded by file path because the runtime imports
each tool.py as a standalone module (no package context).

When to save memory
-------------------
Save a memory when the user says "remember this", when a durable preference
emerges (editor, units, tone, workflow habits), when a project decision is
finalized, when a stable identity/ownership fact surfaces ("Mark owns the
humanmind repo"), when a recurring workflow preference appears, or when
material project state changes ("the alpha launch slipped to Q3"). Memory is
for concise, reusable facts — not documents.

Never store: credentials or secrets of any kind (writes are rejected by a
secret guard), temporary troubleshooting output, unverified assumptions,
transcripts or long quotes, and short-lived state (today's error message,
in-progress task status — use the Todo List for that).

Propose vs. write
-----------------
Write directly (memory_write) for clear-cut, durable, non-sensitive facts at
run/conversation/project scope. Use memory_propose instead when content is
sensitive, when confidence is low, or when the fact is global/user-scope and
you are unsure the user wants it kept forever — proposals stay pending until
approved. Note: direct writes at user/profile scope are stored with
approved=0 (pending review); run/conversation/project writes are approved
immediately.

Scopes and visibility
---------------------
run          dies with the run (scope_id = run_id); only visible in that run.
conversation scope_id = conversation_id; never leaks into other conversations.
project      scope_id = project_id; shared by the project's conversations.
profile      scope_id = profile_id; a user-level persona namespace.
user         global (scope_id = 'user'); visible in every query context.
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
_vectorize = _load_lib("vectorize.py", "agentgpt_tools_vectorize")
_secrets_scan = _load_lib("secrets_scan.py", "agentgpt_tools_secrets_scan")

SCOPES = ("run", "conversation", "project", "profile", "user")
USER_SCOPE_ID = "user"
# Promotion ladder: a memory may only move toward broader scope.
SCOPE_RANK = {"run": 0, "conversation": 1, "project": 2, "profile": 3, "user": 4}
# Ranking boost by scope specificity (narrow beats broad when both match).
SCOPE_PRIORITY = {"run": 1.0, "conversation": 0.85, "project": 0.7, "profile": 0.6, "user": 0.5}
SENSITIVITY_LEVELS = {"public": 0, "normal": 1, "sensitive": 2}
PROPOSAL_STATUSES = ("pending", "approved", "rejected")

# Hybrid ranking weights (sum to 1.0). Each component is normalized to [0, 1].
RANK_WEIGHTS = {
    "lexical": 0.30,     # FTS5 bm25, min-max normalized within the candidate set
    "vector": 0.15,      # cosine of feature-hash embeddings, mapped [-1,1] -> [0,1]
    "scope": 0.15,       # SCOPE_PRIORITY
    "importance": 0.12,  # stored 0..1
    "pinned": 0.10,      # 1.0 when pinned
    "recency": 0.08,     # 0.5 ** (age_in_days / 30) on updated_at
    "frequency": 0.05,   # access_count / (access_count + 5)
    "confidence": 0.05,  # stored 0..1
}

RECENCY_HALF_LIFE_DAYS = 30.0
DUPLICATE_COSINE_THRESHOLD = 0.95
DUPLICATE_CANDIDATE_LIMIT = 50
SEARCH_CANDIDATE_LIMIT = 200
SNIPPET_LENGTH = 160

# Secret guard: memory must never store credentials. Detection lives in
# `tools/_lib/secrets_scan.py` (shared with the knowledge tools); there is
# deliberately no override.


class MemoryToolError(ValueError):
    """Any memory-tool failure; `code` becomes the result's error code."""

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
        raise MemoryToolError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise MemoryToolError("db_unavailable", str(exc)) from exc


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
        raise MemoryToolError("validation_error", "tags must be a list of non-empty strings")
    # De-duplicate, keep order.
    return list(dict.fromkeys(t.strip() for t in tags))


def _normalize_unit_interval(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MemoryToolError("validation_error", f"{field} must be a number in [0, 1]") from exc
    return max(0.0, min(1.0, number))


def _normalize_sensitivity(value: Any) -> str:
    if value is None:
        return "normal"
    if not isinstance(value, str) or value not in SENSITIVITY_LEVELS:
        raise MemoryToolError(
            "validation_error", f"sensitivity must be one of {tuple(SENSITIVITY_LEVELS)}"
        )
    return value


def _normalize_scopes(scopes: Any) -> list[str]:
    if scopes is None:
        return list(SCOPES)
    if isinstance(scopes, str):
        scopes = [scopes]
    if not isinstance(scopes, list) or any(s not in SCOPES for s in scopes):
        raise MemoryToolError("validation_error", f"scopes must be a list from {SCOPES}")
    return list(dict.fromkeys(scopes))


def _parse_timestamp(value: Any, field: str) -> str | None:
    """Normalize an RFC3339 timestamp to UTC ISO format (None passes through)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MemoryToolError("validation_error", f"{field} must be an RFC3339 timestamp string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryToolError("validation_error", f"{field} is not a valid timestamp: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _guard_secrets(content: str) -> None:
    """Reject content that looks like a credential — memory never stores secrets."""
    label = _secrets_scan.scan_for_secret(content)
    if label is not None:
        raise MemoryToolError(
            "sensitive_content_rejected",
            f"content looks like a credential ({label}); memories must never store secrets",
        )


def _normalize_for_compare(content: str) -> str:
    return " ".join(_vectorize.tokens(content))


def _embedding_source(content: str, key: str | None, tags: list[str]) -> str:
    parts = [content]
    if key:
        parts.append(key)
    parts.extend(tags)
    return " ".join(parts)


def _resolve_scope_id(
    conn: sqlite3.Connection,
    scope: str,
    project_id: str | None,
    conversation_id: str | None,
    profile_id: str | None,
) -> str:
    """Resolve the scope_id for a write, defaulting ids from the run context."""
    ctx = _context.get_run_context()
    if scope == "user":
        return USER_SCOPE_ID
    if scope == "run":
        run_id = ctx.get("run_id")
        if not run_id:
            raise MemoryToolError(
                "missing_scope_id", "run scope requires an active run context (no run_id available)"
            )
        return run_id
    if scope == "conversation":
        conversation_id = conversation_id or ctx.get("conversation_id")
        if not conversation_id:
            raise MemoryToolError(
                "missing_scope_id",
                "conversation scope requires a conversation_id (argument or run context)",
            )
        return conversation_id
    if scope == "project":
        conversation_id = conversation_id or ctx.get("conversation_id")
        if not project_id and conversation_id:
            project_id = _db.project_id_for_conversation(conn, conversation_id)
        if not project_id:
            raise MemoryToolError(
                "missing_scope_id",
                "project scope requires a project_id (or a conversation that belongs to a project)",
            )
        return project_id
    # profile
    if not profile_id:
        raise MemoryToolError("missing_scope_id", "profile scope requires a profile_id")
    return profile_id


def _memory_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "memory_id": row["id"],
        "scope": row["scope"],
        "scope_id": row["scope_id"],
        "key": row["memory_key"],
        "content": row["content"],
        "summary": row["summary"],
        "tags": _parse_json(row["tags_json"], []),
        "importance": row["importance"],
        "confidence": row["confidence"],
        "sensitivity": row["sensitivity"],
        "source_type": row["source_type"],
        "source_message_id": row["source_message_id"],
        "provenance": _parse_json(row["provenance_json"]),
        "approved": bool(row["approved"]),
        "pinned": bool(row["pinned"]),
        "access_count": row["access_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_accessed_at": row["last_accessed_at"],
        "expires_at": row["expires_at"],
        "superseded_by": row["superseded_by"],
        "deleted_at": row["deleted_at"],
    }


def _fetch_memory(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise MemoryToolError("not_found", f"memory not found: {memory_id}")
    return row


def _fetch_active_memory(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    row = _fetch_memory(conn, memory_id)
    if row["deleted_at"] is not None:
        raise MemoryToolError("not_found", f"memory is deleted: {memory_id}")
    return row


def _insert_memory(
    conn: sqlite3.Connection,
    *,
    content: str,
    scope: str,
    scope_id: str,
    key: str | None,
    tags: list[str],
    importance: float,
    confidence: float,
    sensitivity: str,
    expires_at: str | None,
    pinned: bool,
    approved: bool,
    source_type: str,
    source_message_id: str | None,
    provenance: Any,
) -> str:
    memory_id = _new_id("mem")
    embedding_source = _embedding_source(content, key, tags)
    embedding = _vectorize.vectorize(embedding_source)
    now = _now()
    conn.execute(
        "INSERT INTO memories (id, scope, scope_id, memory_key, content, summary, tags_json,"
        " lexical_text, embedding_json, embedding_version, importance, confidence, sensitivity,"
        " source_type, source_message_id, provenance_json, approved, pinned, access_count,"
        " created_at, updated_at, last_accessed_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            memory_id,
            scope,
            scope_id,
            key,
            content,
            content[:120],
            _json(tags),
            " ".join(_vectorize.tokens(embedding_source)),
            _vectorize.to_json(embedding),
            _vectorize.EMBEDDING_VERSION,
            importance,
            confidence,
            sensitivity,
            source_type,
            source_message_id,
            _json(provenance),
            1 if approved else 0,
            1 if pinned else 0,
            now,
            now,
            now,
            expires_at,
        ),
    )
    return memory_id


# ── search ──────────────────────────────────────────────────────────────────


def _visibility_clause(
    scopes: list[str],
    project_id: str | None,
    conversation_id: str | None,
    profile_id: str | None,
    run_id: str | None,
) -> tuple[str | None, list[Any]]:
    """SQL fragment limiting rows to what this query context may see.

    user-scope is always visible; profile-scope is visible everywhere unless
    the caller passes a profile_id to restrict to one persona; project,
    conversation, and run scopes are visible only on an exact id match with
    the query context (so a missing context id hides that scope entirely).
    """
    parts: list[str] = []
    params: list[Any] = []
    if "user" in scopes:
        parts.append("m.scope = 'user'")
    if "profile" in scopes:
        if profile_id:
            parts.append("(m.scope = 'profile' AND m.scope_id = ?)")
            params.append(profile_id)
        else:
            parts.append("m.scope = 'profile'")
    if "project" in scopes and project_id:
        parts.append("(m.scope = 'project' AND m.scope_id = ?)")
        params.append(project_id)
    if "conversation" in scopes and conversation_id:
        parts.append("(m.scope = 'conversation' AND m.scope_id = ?)")
        params.append(conversation_id)
    if "run" in scopes and run_id:
        parts.append("(m.scope = 'run' AND m.scope_id = ?)")
        params.append(run_id)
    if not parts:
        return None, []
    return "(" + " OR ".join(parts) + ")", params


def _fts_match_query(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression (OR over tokenized terms)."""
    terms = _vectorize.tokens(query)
    if not terms:
        return None
    # Tokens are alphanumeric-only, so quoting them is injection-safe.
    return " OR ".join(f'"{term}"' for term in terms)


def _recency_score(updated_at: str | None) -> float:
    if not updated_at:
        return 0.5
    try:
        age_seconds = (datetime.now(UTC) - datetime.fromisoformat(updated_at)).total_seconds()
    except ValueError:
        return 0.5
    age_days = max(0.0, age_seconds / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def _score_row(
    row: sqlite3.Row,
    lexical: float,
    query_vector: list[float] | None,
) -> tuple[float, dict[str, float]]:
    vector_score = 0.0
    if query_vector is not None:
        stored = _vectorize.from_json(row["embedding_json"])
        if stored is not None:
            vector_score = (_vectorize.cosine(query_vector, stored) + 1.0) / 2.0
    access_count = row["access_count"] or 0
    components = {
        "lexical": lexical,
        "vector": vector_score,
        "scope": SCOPE_PRIORITY.get(row["scope"], 0.5),
        "importance": max(0.0, min(1.0, row["importance"] or 0.0)),
        "pinned": 1.0 if row["pinned"] else 0.0,
        "recency": _recency_score(row["updated_at"]),
        "frequency": access_count / (access_count + 5.0),
        "confidence": max(0.0, min(1.0, row["confidence"] or 0.0)),
    }
    total = sum(RANK_WEIGHTS[name] * value for name, value in components.items())
    return total, components


def search_memories(
    query: str,
    scopes: Any = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: Any = None,
    sensitivity_maximum: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Hybrid-ranked memory search; see the memory_search wrapper for docs."""
    if not isinstance(query, str):
        raise MemoryToolError("validation_error", "query must be a string")
    scopes = _normalize_scopes(scopes)
    tags = _normalize_tags(tags)
    if sensitivity_maximum is not None and sensitivity_maximum not in SENSITIVITY_LEVELS:
        raise MemoryToolError(
            "validation_error", f"sensitivity_maximum must be one of {tuple(SENSITIVITY_LEVELS)}"
        )
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise MemoryToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > 50:
        raise MemoryToolError("validation_error", "limit must be between 1 and 50")

    conn = _connect()
    try:
        ctx = _context.get_run_context()
        run_id = ctx.get("run_id")
        if not conversation_id:
            conversation_id = ctx.get("conversation_id")
        if not project_id and conversation_id:
            project_id = _db.project_id_for_conversation(conn, conversation_id)

        visibility, params = _visibility_clause(
            scopes, project_id, conversation_id, profile_id, run_id
        )
        if visibility is None:
            return _ok(
                "no memories visible in this context",
                {"hits": [], "count": 0, "context": _search_context(project_id, conversation_id, profile_id, run_id)},
            )

        where = ["m.deleted_at IS NULL", "(m.expires_at IS NULL OR m.expires_at > ?)", visibility]
        base_params: list[Any] = [_now(), *params]
        if sensitivity_maximum is not None:
            allowed = [
                name for name, level in SENSITIVITY_LEVELS.items()
                if level <= SENSITIVITY_LEVELS[sensitivity_maximum]
            ]
            placeholders = ", ".join("?" for _ in allowed)
            where.append(f"(m.sensitivity IS NULL OR m.sensitivity IN ({placeholders}))")
            base_params.extend(allowed)
        for tag in tags:
            where.append("m.tags_json LIKE ?")
            base_params.append(f'%"{tag}"%')

        match = _fts_match_query(query)
        candidate_cap = max(limit * 10, 100)
        if match is not None:
            sql = (
                "SELECT m.*, f.rank AS fts_rank FROM memories_fts f"
                " JOIN memories m ON m.id = f.memory_id"
                f" WHERE memories_fts MATCH ? AND {' AND '.join(where)}"
                " ORDER BY f.rank LIMIT ?"
            )
            rows = conn.execute(sql, (match, *base_params, SEARCH_CANDIDATE_LIMIT)).fetchall()
        else:
            sql = (
                f"SELECT m.*, NULL AS fts_rank FROM memories m"
                f" WHERE {' AND '.join(where)}"
                " ORDER BY m.updated_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (*base_params, candidate_cap)).fetchall()

        # Min-max normalize the FTS rank (SQLite returns <= 0; lower is better).
        raw_lexicals = [-(row["fts_rank"] or 0.0) for row in rows]
        max_lexical = max(raw_lexicals) if raw_lexicals else 0.0
        query_vector = _vectorize.vectorize(query) if query.strip() else None

        scored: list[tuple[float, dict[str, float], sqlite3.Row]] = []
        for row, raw in zip(rows, raw_lexicals):
            lexical = raw / max_lexical if max_lexical > 0 else 0.0
            total, components = _score_row(row, lexical, query_vector)
            scored.append((total, components, row))
        scored.sort(key=lambda item: (item[0], item[2]["updated_at"] or ""), reverse=True)
        top = scored[:limit]

        hits: list[dict[str, Any]] = []
        for total, components, row in top:
            content = row["content"]
            snippet = content if len(content) <= SNIPPET_LENGTH else content[: SNIPPET_LENGTH - 1] + "…"
            hits.append(
                {
                    "memory_id": row["id"],
                    "scope": row["scope"],
                    "scope_id": row["scope_id"],
                    "key": row["memory_key"],
                    "snippet": snippet,
                    "tags": _parse_json(row["tags_json"], []),
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "sensitivity": row["sensitivity"],
                    "approved": bool(row["approved"]),
                    "pinned": bool(row["pinned"]),
                    "access_count": row["access_count"],
                    "updated_at": row["updated_at"],
                    "score": round(total, 4),
                    "score_components": {name: round(value, 4) for name, value in components.items()},
                }
            )

        if top:
            now = _now()
            for _, _, row in top:
                conn.execute(
                    "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ?"
                    " WHERE id = ?",
                    (now, row["id"]),
                )
        conn.commit()
        return _ok(
            f"{len(hits)} memory hit(s)",
            {
                "hits": hits,
                "count": len(hits),
                "context": _search_context(project_id, conversation_id, profile_id, run_id),
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _search_context(
    project_id: str | None,
    conversation_id: str | None,
    profile_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "conversation_id": conversation_id,
        "profile_id": profile_id,
        "run_id": run_id,
    }


# ── proposals & writes ──────────────────────────────────────────────────────


def propose_memory(
    content: str,
    scope: str,
    key: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: Any = None,
    importance: float = 0.5,
    confidence: float = 0.7,
    sensitivity: str = "normal",
    expires_at: str | None = None,
    source_message_id: str | None = None,
    provenance: Any = None,
) -> dict[str, Any]:
    """Queue a memory for user review; see the memory_propose wrapper."""
    content = _require_text(content, "content")
    if scope not in SCOPES:
        raise MemoryToolError("validation_error", f"scope must be one of {SCOPES}")
    tags = _normalize_tags(tags)
    importance = _normalize_unit_interval(importance, "importance")
    confidence = _normalize_unit_interval(confidence, "confidence")
    sensitivity = _normalize_sensitivity(sensitivity)
    expires_at = _parse_timestamp(expires_at, "expires_at")
    provenance_payload = dict(provenance) if isinstance(provenance, dict) else ({"value": provenance} if provenance is not None else {})
    if source_message_id:
        provenance_payload["source_message_id"] = source_message_id

    conn = _connect()
    try:
        scope_id = _resolve_scope_id(conn, scope, project_id, conversation_id, profile_id)
        proposal_id = _new_id("mprop")
        conn.execute(
            "INSERT INTO memory_proposals (id, content, scope, scope_id, memory_key, tags_json,"
            " importance, confidence, sensitivity, expires_at, provenance_json, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                proposal_id,
                content,
                scope,
                scope_id,
                key,
                _json(tags),
                importance,
                confidence,
                sensitivity,
                expires_at,
                _json(provenance_payload),
                _now(),
            ),
        )
        conn.commit()
        return _ok(
            f"proposal {proposal_id} pending review ({scope} scope)",
            {"proposal_id": proposal_id, "status": "pending", "scope": scope, "scope_id": scope_id},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _find_keyed(conn: sqlite3.Connection, scope: str, scope_id: str, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM memories WHERE scope = ? AND scope_id = ? AND memory_key = ?"
        " AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (scope, scope_id, key),
    ).fetchone()


def _find_duplicate(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: str,
    content: str,
    embedding: list[float],
) -> sqlite3.Row | None:
    """Near-identical active memory in the same scope, if one exists."""
    candidates = conn.execute(
        "SELECT * FROM memories WHERE scope = ? AND scope_id = ? AND deleted_at IS NULL"
        " ORDER BY updated_at DESC LIMIT ?",
        (scope, scope_id, DUPLICATE_CANDIDATE_LIMIT),
    ).fetchall()
    normalized = _normalize_for_compare(content)
    for row in candidates:
        if _normalize_for_compare(row["content"]) == normalized:
            return row
    for row in candidates:
        stored = _vectorize.from_json(row["embedding_json"])
        if stored is not None and _vectorize.cosine(embedding, stored) >= DUPLICATE_COSINE_THRESHOLD:
            return row
    return None


def _supersede(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Keep history: mark the old row superseded and soft-delete it."""
    now = _now()
    conn.execute(
        "UPDATE memories SET superseded_by = ?, deleted_at = ?, updated_at = ? WHERE id = ?",
        (new_id, now, now, old_id),
    )


def write_memory(
    proposal_id: str | None = None,
    content: str | None = None,
    scope: str | None = None,
    key: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: Any = None,
    importance: float = 0.5,
    confidence: float = 0.7,
    sensitivity: str = "normal",
    expires_at: str | None = None,
    pinned: bool = False,
    source_message_id: str | None = None,
    provenance: Any = None,
) -> dict[str, Any]:
    """Write a memory (directly or by approving a proposal); see memory_write."""
    conn = _connect()
    try:
        approved = True
        source_type = "agent"
        if proposal_id is not None:
            proposal = conn.execute(
                "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise MemoryToolError("not_found", f"proposal not found: {proposal_id}")
            if proposal["status"] != "pending":
                raise MemoryToolError(
                    "invalid_state",
                    f"proposal {proposal_id} is {proposal['status']}, not pending",
                )
            content = proposal["content"]
            scope = proposal["scope"] or scope
            key = proposal["memory_key"]
            tags = _parse_json(proposal["tags_json"], [])
            importance = proposal["importance"] if proposal["importance"] is not None else 0.5
            confidence = proposal["confidence"] if proposal["confidence"] is not None else 0.7
            sensitivity = proposal["sensitivity"] or "normal"
            expires_at = proposal["expires_at"]
            provenance = _parse_json(proposal["provenance_json"])
            resolved_scope_id = proposal["scope_id"]
            source_type = "proposal"
        else:
            resolved_scope_id = None

        content = _require_text(content, "content")
        if scope not in SCOPES:
            raise MemoryToolError("validation_error", f"scope must be one of {SCOPES}")
        _guard_secrets(content)
        tags = _normalize_tags(tags)
        importance = _normalize_unit_interval(importance, "importance")
        confidence = _normalize_unit_interval(confidence, "confidence")
        sensitivity = _normalize_sensitivity(sensitivity)
        expires_at = _parse_timestamp(expires_at, "expires_at")
        if resolved_scope_id is None:
            resolved_scope_id = _resolve_scope_id(conn, scope, project_id, conversation_id, profile_id)
            # Direct writes to user/profile scope start unapproved (pending review).
            approved = scope in ("run", "conversation", "project")

        embedding = _vectorize.vectorize(_embedding_source(content, key, tags))

        duplicate_of: str | None = None
        superseded: str | None = None
        existing = _find_keyed(conn, scope, resolved_scope_id, key) if key else None
        if existing is not None:
            # Keyed upsert: new row carries the value; old row keeps history.
            if not tags:
                tags = _parse_json(existing["tags_json"], [])
            importance = max(importance, existing["importance"] or 0.0)
            pinned = bool(pinned or existing["pinned"])
            duplicate_of = existing["id"]
        elif (duplicate := _find_duplicate(conn, scope, resolved_scope_id, content, embedding)) is not None:
            # Duplicate suppression: refresh the existing fact instead of cloning it.
            tags = sorted(set(tags) | set(_parse_json(duplicate["tags_json"], [])))
            importance = max(importance, duplicate["importance"] or 0.0)
            confidence = max(confidence, duplicate["confidence"] or 0.0)
            pinned = bool(pinned or duplicate["pinned"])
            duplicate_of = duplicate["id"]
        if duplicate_of is not None:
            approved = bool(
                conn.execute("SELECT approved FROM memories WHERE id = ?", (duplicate_of,)).fetchone()[0]
            ) or approved

        memory_id = _insert_memory(
            conn,
            content=content,
            scope=scope,
            scope_id=resolved_scope_id,
            key=key,
            tags=tags,
            importance=importance,
            confidence=confidence,
            sensitivity=sensitivity,
            expires_at=expires_at,
            pinned=pinned,
            approved=approved,
            source_type=source_type,
            source_message_id=source_message_id,
            provenance=provenance,
        )
        if duplicate_of is not None:
            _supersede(conn, duplicate_of, memory_id)
            superseded = duplicate_of
        if proposal_id is not None:
            conn.execute(
                "UPDATE memory_proposals SET status = 'approved', resolved_at = ? WHERE id = ?",
                (_now(), proposal_id),
            )
        conn.commit()
        action = "updated" if superseded else "saved"
        return _ok(
            f"memory {memory_id} {action} ({scope} scope)"
            + ("" if approved else ", pending review"),
            {
                "memory_id": memory_id,
                "scope": scope,
                "scope_id": resolved_scope_id,
                "key": key,
                "approved": approved,
                "updated": superseded is not None,
                "duplicate_of": duplicate_of,
                "proposal_id": proposal_id,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── read / update / delete / list ───────────────────────────────────────────


def get_memory(memory_id: str) -> dict[str, Any]:
    memory_id = _require_text(memory_id, "memory_id")
    conn = _connect()
    try:
        row = _fetch_memory(conn, memory_id)
        data = _memory_to_dict(row)
        return _ok(
            f"memory {memory_id} ({row['scope']} scope)"
            + (" [deleted]" if row["deleted_at"] else ""),
            data,
        )
    finally:
        conn.close()


def update_memory(
    memory_id: str,
    content: str | None = None,
    key: str | None = None,
    scope: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: Any = None,
    importance: float | None = None,
    confidence: float | None = None,
    sensitivity: str | None = None,
    expires_at: str | None = None,
    pinned: bool | None = None,
) -> dict[str, Any]:
    memory_id = _require_text(memory_id, "memory_id")
    if content is not None:
        content = _require_text(content, "content")
        _guard_secrets(content)
    if scope is not None and scope not in SCOPES:
        raise MemoryToolError("validation_error", f"scope must be one of {SCOPES}")
    tags_value = _normalize_tags(tags) if tags is not None else None
    if importance is not None:
        importance = _normalize_unit_interval(importance, "importance")
    if confidence is not None:
        confidence = _normalize_unit_interval(confidence, "confidence")
    if sensitivity is not None:
        sensitivity = _normalize_sensitivity(sensitivity)
    expires_value = _parse_timestamp(expires_at, "expires_at") if expires_at is not None else None

    conn = _connect()
    try:
        row = _fetch_active_memory(conn, memory_id)
        new_scope = scope or row["scope"]
        if scope is not None and scope != row["scope"]:
            # Moving scopes: validate the target scope's id is determinable.
            if scope == "user":
                new_scope_id = USER_SCOPE_ID
            elif scope == "conversation":
                new_scope_id = conversation_id or (
                    row["scope_id"] if row["scope"] == "conversation" else None
                ) or _context.get_run_context().get("conversation_id")
            elif scope == "project":
                candidate_conversation = conversation_id or (
                    row["scope_id"] if row["scope"] == "conversation" else None
                )
                new_scope_id = project_id or (
                    _db.project_id_for_conversation(conn, candidate_conversation)
                    if candidate_conversation
                    else None
                )
            elif scope == "profile":
                new_scope_id = profile_id or (
                    row["scope_id"] if row["scope"] == "profile" else None
                )
            else:  # run
                new_scope_id = _context.get_run_context().get("run_id")
            if not new_scope_id:
                raise MemoryToolError(
                    "missing_scope_id", f"cannot move memory to {scope} scope: no target id available"
                )
        else:
            new_scope_id = row["scope_id"]

        new_content = content if content is not None else row["content"]
        new_key = key if key is not None else row["memory_key"]
        new_tags = tags_value if tags_value is not None else _parse_json(row["tags_json"], [])
        new_importance = importance if importance is not None else (row["importance"] or 0.0)
        new_confidence = confidence if confidence is not None else (row["confidence"] or 0.0)
        new_sensitivity = sensitivity if sensitivity is not None else (row["sensitivity"] or "normal")
        new_expires = expires_value if expires_at is not None else row["expires_at"]
        new_pinned = pinned if pinned is not None else bool(row["pinned"])

        embedding_source = _embedding_source(new_content, new_key, new_tags)
        embedding = _vectorize.vectorize(embedding_source)
        conn.execute(
            "UPDATE memories SET scope = ?, scope_id = ?, memory_key = ?, content = ?, summary = ?,"
            " tags_json = ?, lexical_text = ?, embedding_json = ?, embedding_version = ?,"
            " importance = ?, confidence = ?, sensitivity = ?, expires_at = ?, pinned = ?,"
            " updated_at = ? WHERE id = ?",
            (
                new_scope,
                new_scope_id,
                new_key,
                new_content,
                new_content[:120],
                _json(new_tags),
                " ".join(_vectorize.tokens(embedding_source)),
                _vectorize.to_json(embedding),
                _vectorize.EMBEDDING_VERSION,
                new_importance,
                new_confidence,
                new_sensitivity,
                new_expires,
                1 if new_pinned else 0,
                _now(),
                memory_id,
            ),
        )
        conn.commit()
        moved = new_scope != row["scope"] or new_scope_id != row["scope_id"]
        return _ok(
            f"memory {memory_id} updated" + (f" (moved to {new_scope} scope)" if moved else ""),
            {
                "memory_id": memory_id,
                "scope": new_scope,
                "scope_id": new_scope_id,
                "moved": moved,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_memory(memory_id: str) -> dict[str, Any]:
    memory_id = _require_text(memory_id, "memory_id")
    conn = _connect()
    try:
        row = _fetch_memory(conn, memory_id)
        if row["deleted_at"] is not None:
            return _ok(
                f"memory {memory_id} was already deleted",
                {"memory_id": memory_id, "deleted": True, "already_deleted": True},
            )
        conn.execute(
            "UPDATE memories SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), memory_id),
        )
        conn.commit()
        return _ok(
            f"memory {memory_id} deleted",
            {"memory_id": memory_id, "deleted": True, "already_deleted": False},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_memories(
    scopes: Any = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: Any = None,
    pinned: bool | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    scopes = _normalize_scopes(scopes)
    tags = _normalize_tags(tags)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise MemoryToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > 100:
        raise MemoryToolError("validation_error", "limit must be between 1 and 100")

    conn = _connect()
    try:
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        placeholders = ", ".join("?" for _ in scopes)
        clauses.append(f"scope IN ({placeholders})")
        params.extend(scopes)
        if project_id:
            clauses.append("(scope = 'project' AND scope_id = ?)")
            params.append(project_id)
        if conversation_id:
            clauses.append("(scope = 'conversation' AND scope_id = ?)")
            params.append(conversation_id)
        if profile_id:
            clauses.append("(scope = 'profile' AND scope_id = ?)")
            params.append(profile_id)
        for tag in tags:
            clauses.append("tags_json LIKE ?")
            params.append(f'%"{tag}"%')
        if pinned is not None:
            clauses.append("pinned = ?")
            params.append(1 if pinned else 0)
        if cursor:
            try:
                cursor_created, cursor_id = cursor.split("|", 1)
            except ValueError as exc:
                raise MemoryToolError("validation_error", "malformed cursor") from exc
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created, cursor_created, cursor_id])
        rows = conn.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        memories = [_memory_to_dict(row) for row in rows]
        next_cursor = None
        if len(rows) == limit:
            last = rows[-1]
            next_cursor = f"{last['created_at']}|{last['id']}"
        return _ok(
            f"{len(memories)} memor{'y' if len(memories) == 1 else 'ies'}",
            {"memories": memories, "count": len(memories), "next_cursor": next_cursor},
        )
    finally:
        conn.close()


# ── promote / merge / mark-used ─────────────────────────────────────────────


def promote_memory(
    memory_id: str,
    destination_scope: str,
    project_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    memory_id = _require_text(memory_id, "memory_id")
    if destination_scope not in SCOPES:
        raise MemoryToolError("validation_error", f"destination_scope must be one of {SCOPES}")
    conn = _connect()
    try:
        row = _fetch_active_memory(conn, memory_id)
        source_scope = row["scope"]
        if SCOPE_RANK[destination_scope] <= SCOPE_RANK[source_scope]:
            raise MemoryToolError(
                "invalid_promotion",
                f"cannot promote {source_scope} -> {destination_scope}: destination must be broader",
            )
        if destination_scope == "user":
            new_scope_id = USER_SCOPE_ID
        elif destination_scope == "profile":
            new_scope_id = profile_id
        elif destination_scope == "project":
            candidate_conversation = row["scope_id"] if source_scope == "conversation" else None
            new_scope_id = project_id or (
                _db.project_id_for_conversation(conn, candidate_conversation)
                if candidate_conversation
                else None
            )
        elif destination_scope == "conversation":
            new_scope_id = _context.get_run_context().get("conversation_id")
        else:  # run — only reachable from nothing broader, but guard anyway
            new_scope_id = _context.get_run_context().get("run_id")
        if not new_scope_id:
            raise MemoryToolError(
                "missing_scope_id",
                f"cannot promote to {destination_scope} scope: no target id available",
            )
        conn.execute(
            "UPDATE memories SET scope = ?, scope_id = ?, updated_at = ? WHERE id = ?",
            (destination_scope, new_scope_id, _now(), memory_id),
        )
        conn.commit()
        return _ok(
            f"memory {memory_id} promoted {source_scope} -> {destination_scope}",
            {
                "memory_id": memory_id,
                "scope": destination_scope,
                "scope_id": new_scope_id,
                "previous_scope": source_scope,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def merge_memories(
    memory_ids: list[str],
    merged_content: str,
    destination_scope: str | None = None,
) -> dict[str, Any]:
    if not isinstance(memory_ids, list) or len(memory_ids) < 2:
        raise MemoryToolError("validation_error", "memory_ids must list at least two memories")
    memory_ids = [_require_text(mid, "memory_id") for mid in memory_ids]
    if len(set(memory_ids)) != len(memory_ids):
        raise MemoryToolError("validation_error", "memory_ids must be distinct")
    merged_content = _require_text(merged_content, "merged_content")
    _guard_secrets(merged_content)
    if destination_scope is not None and destination_scope not in SCOPES:
        raise MemoryToolError("validation_error", f"destination_scope must be one of {SCOPES}")

    conn = _connect()
    try:
        rows = [_fetch_active_memory(conn, mid) for mid in memory_ids]
        target_scope = destination_scope or max((r["scope"] for r in rows), key=SCOPE_RANK.get)
        at_target = [r for r in rows if r["scope"] == target_scope]
        if at_target:
            distinct_ids = {r["scope_id"] for r in at_target}
            if len(distinct_ids) > 1 and destination_scope is None:
                raise MemoryToolError(
                    "scope_mismatch",
                    f"sources disagree on {target_scope} scope_id; pass destination_scope to merge broader",
                )
            scope_id = at_target[0]["scope_id"]
        elif target_scope == "user":
            scope_id = USER_SCOPE_ID
        else:
            raise MemoryToolError(
                "scope_mismatch",
                f"no source at {target_scope} scope; cannot determine target scope_id",
            )

        tags = sorted({tag for r in rows for tag in _parse_json(r["tags_json"], [])})
        importance = max((r["importance"] or 0.0) for r in rows)
        confidence = sum((r["confidence"] or 0.0) for r in rows) / len(rows)
        sensitivity = max((r["sensitivity"] or "normal" for r in rows), key=SENSITIVITY_LEVELS.get)
        pinned = any(r["pinned"] for r in rows)
        approved = (
            True
            if target_scope in ("run", "conversation", "project")
            else all(r["approved"] for r in rows)
        )
        provenance = {"merged_from": memory_ids}
        memory_id = _insert_memory(
            conn,
            content=merged_content,
            scope=target_scope,
            scope_id=scope_id,
            key=None,
            tags=tags,
            importance=importance,
            confidence=round(confidence, 4),
            sensitivity=sensitivity,
            expires_at=None,
            pinned=pinned,
            approved=approved,
            source_type="merge",
            source_message_id=None,
            provenance=provenance,
        )
        for row in rows:
            _supersede(conn, row["id"], memory_id)
        conn.commit()
        return _ok(
            f"{len(rows)} memories merged into {memory_id} ({target_scope} scope)",
            {
                "memory_id": memory_id,
                "scope": target_scope,
                "scope_id": scope_id,
                "merged_ids": memory_ids,
                "approved": approved,
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_used(memory_id: str) -> dict[str, Any]:
    memory_id = _require_text(memory_id, "memory_id")
    conn = _connect()
    try:
        row = _fetch_active_memory(conn, memory_id)
        conn.execute(
            "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
            (_now(), memory_id),
        )
        conn.commit()
        return _ok(
            f"memory {memory_id} marked used",
            {"memory_id": memory_id, "access_count": (row["access_count"] or 0) + 1},
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
    except MemoryToolError as exc:
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
def memory_search(
    query: str,
    scopes: list[str] | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: list[str] | None = None,
    sensitivity_maximum: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Search memories relevant to a query before answering or when recalling.

    Call this when the answer may depend on user preferences, prior decisions,
    or project facts you may have stored earlier. Results are ranked by a
    hybrid score: FTS5 lexical match (0.30) + feature-vector cosine (0.15) +
    scope specificity (0.15) + importance (0.12) + pinned (0.10) + recency
    (0.08) + access frequency (0.05) + confidence (0.05).

    Visibility: user-scope memories always match; project/conversation/run
    scopes only match their own ids; run-scope memories never outlive their
    run. Expired, soft-deleted, and over-sensitivity memories are excluded.
    Returned memories get their access counters bumped automatically.

    Args:
        query: Natural-language search text.
        scopes: Restrict to a subset of run/conversation/project/profile/user
            (default: all visible scopes).
        project_id: Project context (defaults from the conversation).
        conversation_id: Conversation context (defaults from the run).
        profile_id: Restrict profile-scope memories to one profile.
        tags: Only memories carrying all of these tags.
        sensitivity_maximum: Exclude memories above this level
            (public < normal < sensitive).
        limit: Maximum hits (1-50, default 8).

    Returns:
        `{ok, summary, data: {hits: [{memory_id, scope, key, snippet, score,
        score_components, ...}], count, context}, error}`.
    """
    return _wrap(
        search_memories,
        query, scopes, project_id, conversation_id, profile_id, tags,
        sensitivity_maximum, limit,
    )


@tool
def memory_propose(
    content: str,
    scope: str,
    key: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: list[str] | None = None,
    importance: float = 0.5,
    confidence: float = 0.7,
    sensitivity: str = "normal",
    expires_at: str | None = None,
    source_message_id: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a memory for user review instead of writing it directly.

    Prefer this over memory_write when the content is sensitive, when your
    confidence is low, or when the fact is user/profile-scope and you are
    unsure the user wants it kept. Clear-cut durable preferences, finalized
    decisions, and stable identity facts at run/conversation/project scope
    can go straight to memory_write. The proposal stays 'pending' until it is
    written via memory_write(proposal_id=...) or rejected by the user.

    Args:
        content: The concise fact to remember (never credentials).
        scope: run | conversation | project | profile | user.
        key: Optional stable key for keyed upsert (e.g. "editor-preference").
        project_id: Required for project scope unless resolvable from the
            conversation.
        conversation_id: Defaults from the run context.
        profile_id: Required for profile scope.
        tags: Free-form labels for filtering.
        importance: 0..1, default 0.5.
        confidence: 0..1, default 0.7.
        sensitivity: public | normal | sensitive (default normal).
        expires_at: Optional RFC3339 expiry.
        source_message_id: Message the fact came from (audit trail).
        provenance: Optional extra provenance metadata.

    Returns:
        `{ok, summary, data: {proposal_id, status, scope, scope_id}, error}`.
    """
    return _wrap(
        propose_memory,
        content, scope, key, project_id, conversation_id, profile_id, tags,
        importance, confidence, sensitivity, expires_at, source_message_id, provenance,
    )


@tool
def memory_write(
    proposal_id: str | None = None,
    content: str | None = None,
    scope: str | None = None,
    key: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: list[str] | None = None,
    importance: float = 0.5,
    confidence: float = 0.7,
    sensitivity: str = "normal",
    expires_at: str | None = None,
    pinned: bool = False,
    source_message_id: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save a memory, or approve a pending proposal by id.

    Two modes: (a) pass proposal_id to approve a pending proposal and write
    it (all fields come from the proposal); (b) pass content + scope for a
    direct write. Save durable preferences, finalized decisions, stable
    identity/ownership facts, recurring workflow preferences, and material
    project state changes. Never store credentials — content that looks like
    a password, API key, token, or private key is rejected with
    'sensitive_content_rejected' (no override). Prefer memory_propose for
    sensitive or low-confidence content.

    Direct writes at run/conversation/project scope are approved immediately;
    direct writes at user/profile scope are stored with approved=false
    (pending user review). If the same (scope, scope_id, key) already exists,
    the old row is superseded (history kept) and the new row takes its place.
    Near-identical content in the same scope updates the existing memory
    instead of duplicating it (data.duplicate_of points at the replaced row).

    Args:
        proposal_id: Pending proposal to approve and write.
        content: The concise fact (required for direct writes).
        scope: run | conversation | project | profile | user.
        key: Optional stable key for keyed upsert.
        project_id / conversation_id / profile_id: Scope ids (default from
            the run context where possible).
        tags: Free-form labels.
        importance: 0..1, default 0.5.
        confidence: 0..1, default 0.7.
        sensitivity: public | normal | sensitive (default normal).
        expires_at: Optional RFC3339 expiry (run-scope facts need none).
        pinned: Pin to boost ranking.
        source_message_id: Message the fact came from.
        provenance: Optional provenance metadata.

    Returns:
        `{ok, summary, data: {memory_id, scope, scope_id, approved, updated,
        duplicate_of, proposal_id}, error}`.
    """
    return _wrap(
        write_memory,
        proposal_id, content, scope, key, project_id, conversation_id, profile_id,
        tags, importance, confidence, sensitivity, expires_at, pinned,
        source_message_id, provenance,
    )


@tool
def memory_get(memory_id: str) -> dict[str, Any]:
    """Fetch one memory with full metadata.

    Args:
        memory_id: The memory id from memory_write or memory_search.

    Returns:
        `{ok, summary, data: {memory fields...}, error}`.
    """
    return _wrap(get_memory, memory_id)


@tool
def memory_update(
    memory_id: str,
    content: str | None = None,
    key: str | None = None,
    scope: str | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: list[str] | None = None,
    importance: float | None = None,
    confidence: float | None = None,
    sensitivity: str | None = None,
    expires_at: str | None = None,
    pinned: bool | None = None,
) -> dict[str, Any]:
    """Patch a memory in place; changing scope moves the row.

    The target scope's id must be determinable (argument, the row's current
    scope, or the run context) or the move fails. New content passes through
    the same secret guard as memory_write.

    Args:
        memory_id: Memory to update.
        content: Replacement content.
        key: Replacement key.
        scope: Move to this scope (run/conversation/project/profile/user).
        project_id / conversation_id / profile_id: Target scope ids.
        tags: Replacement tag list.
        importance / confidence: Replacement 0..1 values.
        sensitivity: public | normal | sensitive.
        expires_at: Replacement RFC3339 expiry.
        pinned: Pin or unpin.

    Returns:
        `{ok, summary, data: {memory_id, scope, scope_id, moved}, error}`.
    """
    return _wrap(
        update_memory,
        memory_id, content, key, scope, project_id, conversation_id, profile_id,
        tags, importance, confidence, sensitivity, expires_at, pinned,
    )


@tool
def memory_delete(memory_id: str) -> dict[str, Any]:
    """Soft-delete a memory (kept for history, excluded from all searches).

    Args:
        memory_id: Memory to delete.

    Returns:
        `{ok, summary, data: {memory_id, deleted, already_deleted}, error}`.
    """
    return _wrap(delete_memory, memory_id)


@tool
def memory_list(
    scopes: list[str] | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
    profile_id: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List memories, newest first, with keyset pagination.

    Args:
        scopes: Restrict to these scopes (default all).
        project_id: Only project memories of this project.
        conversation_id: Only conversation memories of this conversation.
        profile_id: Only profile memories of this profile.
        tags: Only memories carrying all of these tags.
        pinned: Only pinned (true) or unpinned (false) memories.
        limit: Page size (1-100, default 20).
        cursor: next_cursor from a previous page.

    Returns:
        `{ok, summary, data: {memories: [...], count, next_cursor}, error}`.
    """
    return _wrap(
        list_memories,
        scopes, project_id, conversation_id, profile_id, tags, pinned, limit, cursor,
    )


@tool
def memory_promote(
    memory_id: str,
    destination_scope: str,
    project_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Promote a memory to a broader scope (e.g. conversation -> project -> user).

    The ladder is run < conversation < project < profile < user; promotion
    only moves upward — demoting user-scope back to conversation is rejected.

    Args:
        memory_id: Memory to promote.
        destination_scope: Target scope, broader than the current one.
        project_id: Target project (defaults from the source conversation).
        profile_id: Target profile for profile-scope promotion.

    Returns:
        `{ok, summary, data: {memory_id, scope, scope_id, previous_scope}, error}`.
    """
    return _wrap(promote_memory, memory_id, destination_scope, project_id, profile_id)


@tool
def memory_merge(
    memory_ids: list[str],
    merged_content: str,
    destination_scope: str | None = None,
) -> dict[str, Any]:
    """Combine several memories into one consolidated memory.

    Sources are superseded by the new memory and soft-deleted. The merged
    memory lands at destination_scope, or the broadest scope among sources.
    Tags are unioned, importance takes the max, confidence the mean, and
    sensitivity the most restrictive level.

    Args:
        memory_ids: At least two active memory ids.
        merged_content: The consolidated fact (secret guard applies).
        destination_scope: Optional target scope override.

    Returns:
        `{ok, summary, data: {memory_id, scope, scope_id, merged_ids}, error}`.
    """
    return _wrap(merge_memories, memory_ids, merged_content, destination_scope)


@tool
def memory_mark_used(memory_id: str) -> dict[str, Any]:
    """Record that a memory was used (bumps access_count / last_accessed_at).

    memory_search bumps automatically; call this when you relied on a memory
    without finding it through search (e.g. it was injected into context).

    Args:
        memory_id: Memory that proved useful.

    Returns:
        `{ok, summary, data: {memory_id, access_count}, error}`.
    """
    return _wrap(mark_used, memory_id)


TOOL = [
    memory_search,
    memory_propose,
    memory_write,
    memory_get,
    memory_update,
    memory_delete,
    memory_list,
    memory_promote,
    memory_merge,
    memory_mark_used,
]
