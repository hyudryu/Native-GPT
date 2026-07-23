"""Notifications Strands tools — persistent user notifications via the host DB.

Multi-tool folder: `TOOL` is a list of Strands tools. Rows go into the
`notifications` table (migration 0011, opened through `tools/_lib/db.py`).

Delivery model: there is NO direct push channel from the agent runtime. The
host UI polls/subscribes to the `notifications` table and renders unread,
non-dismissed rows; `send_notification` is therefore "read" risk — it only
writes a database row the host chooses to surface. Mark-read and dismiss are
the same rows' lifecycle flags (`read`, `dismissed`); dismissed rows are
excluded from list_notifications entirely.
"""

from __future__ import annotations

import importlib.util
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

URGENCIES = ("low", "normal", "high", "urgent")
LIST_LIMIT_MAX = 100


class NotificationToolError(ValueError):
    """Any notification-tool failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _ok(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotificationToolError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise NotificationToolError("db_unavailable", str(exc)) from exc


def _normalize_limit(limit: Any) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise NotificationToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > LIST_LIMIT_MAX:
        raise NotificationToolError(
            "validation_error", f"limit must be between 1 and {LIST_LIMIT_MAX}"
        )
    return limit


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "notification_id": row["id"],
        "title": row["title"],
        "message": row["message"],
        "urgency": row["urgency"],
        "action_url": row["action_url"],
        "artifact_id": row["artifact_id"],
        "read": bool(row["read"]),
        "dismissed": bool(row["dismissed"]),
        "created_at": row["created_at"],
    }


def _fetch_notification(conn: sqlite3.Connection, notification_id: str) -> sqlite3.Row:
    notification_id = _require_text(notification_id, "notification_id")
    row = conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (notification_id,)
    ).fetchone()
    if row is None:
        raise NotificationToolError("not_found", f"notification not found: {notification_id}")
    return row


# ── plain implementations ───────────────────────────────────────────────────


def send(
    title: str,
    message: str | None = None,
    urgency: str = "normal",
    action_url: str | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Insert a notification row for the host UI to surface; see send_notification."""
    title = _require_text(title, "title")
    if urgency not in URGENCIES:
        raise NotificationToolError(
            "validation_error", f"urgency must be one of {URGENCIES}"
        )
    if message is not None and not isinstance(message, str):
        raise NotificationToolError("validation_error", "message must be a string")
    conn = _connect()
    try:
        notification_id = _new_id("ntf")
        conn.execute(
            "INSERT INTO notifications (id, title, message, urgency, action_url, artifact_id,"
            " read, dismissed, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (notification_id, title, message, urgency, action_url, artifact_id, _now()),
        )
        conn.commit()
        return _ok(
            f"notification sent ({urgency}): {title}",
            {
                "notification_id": notification_id,
                "title": title,
                "urgency": urgency,
                "delivered": "queued",
                "delivery": "host picks rows up from the notifications table",
            },
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_rows(
    unread_only: bool = False, limit: int = 20, cursor: str | None = None
) -> dict[str, Any]:
    """List notifications, newest first, keyset-paginated; dismissed excluded."""
    limit = _normalize_limit(limit)
    conn = _connect()
    try:
        clauses = ["dismissed = 0"]
        params: list[Any] = []
        if unread_only:
            clauses.append("read = 0")
        if cursor:
            try:
                cursor_created, cursor_id = cursor.split("|", 1)
            except ValueError as exc:
                raise NotificationToolError("validation_error", "malformed cursor") from exc
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created, cursor_created, cursor_id])
        rows = conn.execute(
            f"SELECT * FROM notifications WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        next_cursor = None
        if len(rows) == limit:
            last = rows[-1]
            next_cursor = f"{last['created_at']}|{last['id']}"
        return _ok(
            f"{len(rows)} notification(s)",
            {
                "notifications": [_row_to_dict(row) for row in rows],
                "count": len(rows),
                "next_cursor": next_cursor,
            },
        )
    finally:
        conn.close()


def mark_read(notification_id: str) -> dict[str, Any]:
    """Flag a notification as read (it stays listed unless dismissed)."""
    conn = _connect()
    try:
        row = _fetch_notification(conn, notification_id)
        conn.execute("UPDATE notifications SET read = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        return _ok(
            f"notification {row['id']} marked read",
            {"notification_id": row["id"], "read": True, "already_read": bool(row["read"])},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dismiss(notification_id: str) -> dict[str, Any]:
    """Dismiss a notification (excluded from list_notifications afterwards)."""
    conn = _connect()
    try:
        row = _fetch_notification(conn, notification_id)
        conn.execute("UPDATE notifications SET dismissed = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        return _ok(
            f"notification {row['id']} dismissed",
            {
                "notification_id": row["id"],
                "dismissed": True,
                "already_dismissed": bool(row["dismissed"]),
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
    except NotificationToolError as exc:
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
def send_notification(
    title: str,
    message: str | None = None,
    urgency: str = "normal",
    action_url: str | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Queue a user notification (persisted to the host's notifications table).

    Delivery is via the notifications table: the host UI surfaces unread,
    non-dismissed rows — this tool does not push anything itself. Use for
    completed long-running work, important findings, or anything the user
    should see even if they have navigated away. Reserve high/urgent for
    genuinely time-sensitive items.

    Args:
        title: Short headline (required).
        message: Optional body text.
        urgency: low | normal | high | urgent (default normal).
        action_url: Optional in-app link the host UI can offer.
        artifact_id: Optional artifact the notification points at.

    Returns:
        `{ok, summary, data: {notification_id, title, urgency, delivered,
        delivery}, error}`.
    """
    return _wrap(send, title, message, urgency, action_url, artifact_id)


@tool
def list_notifications(
    unread_only: bool = False, limit: int = 20, cursor: str | None = None
) -> dict[str, Any]:
    """List notifications, newest first; dismissed ones are always excluded.

    Args:
        unread_only: Only notifications not yet marked read (default False).
        limit: Page size (1-100, default 20).
        cursor: Keyset cursor from a previous response.

    Returns:
        `{ok, summary, data: {notifications: [...], count, next_cursor},
        error}`.
    """
    return _wrap(list_rows, unread_only, limit, cursor)


@tool
def mark_notification_read(notification_id: str) -> dict[str, Any]:
    """Mark a notification read (it remains listed until dismissed).

    Args:
        notification_id: Notification to mark read.

    Returns:
        `{ok, summary, data: {notification_id, read, already_read}, error}`.
    """
    return _wrap(mark_read, notification_id)


@tool
def dismiss_notification(notification_id: str) -> dict[str, Any]:
    """Dismiss a notification (excluded from list_notifications afterwards).

    Args:
        notification_id: Notification to dismiss.

    Returns:
        `{ok, summary, data: {notification_id, dismissed, already_dismissed},
        error}`.
    """
    return _wrap(dismiss, notification_id)


TOOL = [
    send_notification,
    list_notifications,
    mark_notification_read,
    dismiss_notification,
]
