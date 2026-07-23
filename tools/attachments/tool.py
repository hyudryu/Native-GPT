"""Attachments Strands tools — conversation-scoped files built on the artifact store.

Multi-tool folder: `TOOL` is a list of Strands tools. Attachments are rows in
the `attachments` table (migration 0011) that link a conversation/project to
an artifact; the artifact's bytes live in the artifact store (`tools/artifacts`,
loaded by file path like the `_lib` helpers — the runtime imports each tool.py
standalone, so cross-folder imports go through importlib).

Supported reading/searching formats: text/markdown/code/JSON/CSV — anything
that decodes as UTF-8 with a text-like MIME type. PDF, DOCX, XLSX, and images
are stored and listed, but reading/searching them returns a clear
`unsupported_content` error: a dedicated parser stage (planned) will extract
their text. `search_attachments` is a bounded keyword scan (no FTS table for
attachment content exists and new migrations are out of scope); it scans at
most SEARCH_SCAN_BYTES per attachment.
"""

from __future__ import annotations

import importlib.util
import mimetypes
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands import tool

# Load shared `_lib` helpers and the artifacts tool by file path (no package
# context when the runtime imports this file standalone).
_TOOLS_DIR = Path(__file__).resolve().parent.parent


def _load_module(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_db = _load_module(_TOOLS_DIR / "_lib" / "db.py", "agentgpt_tools_db")
_context = _load_module(_TOOLS_DIR / "_lib" / "context.py", "agentgpt_tools_context")
_paths = _load_module(_TOOLS_DIR / "_lib" / "paths.py", "agentgpt_tools_paths")
_artifacts = _load_module(_TOOLS_DIR / "artifacts" / "tool.py", "agentgpt_tools_artifacts_store")

PAGE_LINES = 200  # fixed page window for read_attachment(page=...)
SEARCH_SCAN_BYTES = 512 * 1024  # per-attachment scan cap for keyword search
SEARCH_CONTEXT_CHARS = 120  # context shown around each match
LIST_LIMIT_MAX = 100

# MIME families we refuse to read/search (parser stage planned).
UNSUPPORTED_MIME_PREFIXES = ("image/", "audio/", "video/")
UNSUPPORTED_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
    "application/x-tar",
    "application/gzip",
}

# Deterministic extension → MIME map. `mimetypes` reads the OS registry on
# Windows (e.g. .csv may come back as application/vnd.ms-excel), which would
# misclassify text formats, so known extensions win over the registry guess.
EXTENSION_MIMES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".ndjson": "application/x-ndjson",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".sql": "application/sql",
    ".log": "text/plain",
    ".py": "text/x-python",
    ".js": "application/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".rs": "text/x-rust",
    ".sh": "application/x-sh",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
}


def _guess_mime(filename: str) -> str:
    known = EXTENSION_MIMES.get(Path(filename).suffix.lower())
    if known:
        return known
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


class AttachmentToolError(ValueError):
    """Any attachment-tool failure; `code` becomes the result's error code."""

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
        raise AttachmentToolError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise AttachmentToolError("db_unavailable", str(exc)) from exc


def _normalize_limit(limit: Any, maximum: int = LIST_LIMIT_MAX) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise AttachmentToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > maximum:
        raise AttachmentToolError("validation_error", f"limit must be between 1 and {maximum}")
    return limit


def _fetch_attachment(conn: sqlite3.Connection, attachment_id: str) -> sqlite3.Row:
    attachment_id = _require_text(attachment_id, "attachment_id")
    row = conn.execute(
        "SELECT a.*, ar.mime_type AS artifact_mime_type, ar.storage_path,"
        " ar.deleted_at AS artifact_deleted_at, ar.sha256, ar.created_by_tool"
        " FROM attachments a LEFT JOIN artifacts ar ON ar.id = a.artifact_id"
        " WHERE a.id = ?",
        (attachment_id,),
    ).fetchone()
    if row is None or row["deleted_at"] is not None:
        raise AttachmentToolError("not_found", f"attachment not found: {attachment_id}")
    return row


def _attachment_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "attachment_id": row["id"],
        "conversation_id": row["conversation_id"],
        "project_id": row["project_id"],
        "artifact_id": row["artifact_id"],
        "name": row["name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
    }


def _check_supported(row: sqlite3.Row) -> None:
    """Refuse reads/searches of formats that need the (planned) parser stage."""
    mime = (row["mime_type"] or "").split(";")[0].strip().lower()
    if mime in UNSUPPORTED_MIMES or any(mime.startswith(p) for p in UNSUPPORTED_MIME_PREFIXES):
        raise AttachmentToolError(
            "unsupported_content",
            f"attachment '{row['name']}' ({mime or 'unknown type'}) cannot be read as text yet; "
            "PDF/DOCX/XLSX/image parsing is a planned stage — the file is stored and listed, "
            "but content access needs the parser",
        )


def _read_attachment_text(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Full text of an attachment (bounded by the artifact store's 50 MB cap)."""
    _check_supported(row)
    if row["artifact_id"] is None or row["artifact_deleted_at"] is not None:
        raise AttachmentToolError(
            "artifact_unavailable", f"artifact for attachment {row['id']} is missing or deleted"
        )
    artifact_row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ?", (row["artifact_id"],)
    ).fetchone()
    try:
        return _artifacts._read_text(artifact_row)
    except _artifacts.ArtifactToolError as exc:
        raise AttachmentToolError(exc.code, str(exc)) from exc


def _window_text(
    text: str,
    name: str,
    page: int | None,
    offset: int,
    length: int | None,
) -> dict[str, Any]:
    """Slice text by fixed 200-line page or by character window."""
    if page is not None:
        try:
            page = int(page)
        except (TypeError, ValueError) as exc:
            raise AttachmentToolError("validation_error", "page must be an integer") from exc
        if page < 1:
            raise AttachmentToolError("validation_error", "page is 1-based (>= 1)")
        lines = text.splitlines()
        total_pages = max(1, (len(lines) + PAGE_LINES - 1) // PAGE_LINES)
        start = (page - 1) * PAGE_LINES
        window = "\n".join(lines[start : start + PAGE_LINES])
        return {
            "content": window,
            "mode": "page",
            "page": page,
            "total_pages": total_pages,
            "total_lines": len(lines),
            "total_chars": len(text),
            "truncated_before": page > 1,
            "truncated_after": page < total_pages,
        }
    if offset < 0:
        raise AttachmentToolError("validation_error", "offset must be a non-negative integer")
    if length is None:
        length = _artifacts.DEFAULT_READ_CHARS
    try:
        length = int(length)
    except (TypeError, ValueError) as exc:
        raise AttachmentToolError("validation_error", "length must be an integer") from exc
    if length < 1 or length > _artifacts.MAX_READ_CHARS:
        raise AttachmentToolError(
            "validation_error",
            f"length must be between 1 and {_artifacts.MAX_READ_CHARS} characters",
        )
    window = text[offset : offset + length]
    return {
        "content": window,
        "mode": "window",
        "offset": offset,
        "length": len(window),
        "total_chars": len(text),
        "truncated_before": offset > 0,
        "truncated_after": offset + len(window) < len(text),
    }


# ── plain implementations ───────────────────────────────────────────────────


def attach(
    conversation_id: str | None = None,
    source_path: str | None = None,
    artifact_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Attach a file (imported as an artifact) or an existing artifact; see attach_file."""
    if (source_path is None) == (artifact_id is None):
        raise AttachmentToolError(
            "validation_error", "provide exactly one of source_path or artifact_id"
        )
    conn = _connect()
    try:
        ctx = _context.get_run_context()
        conversation_id = conversation_id or ctx.get("conversation_id")
        if not project_id and conversation_id:
            project_id = _db.project_id_for_conversation(conn, conversation_id)

        if source_path is not None:
            resolved = _paths.resolve_under_root(_require_text(source_path, "source_path"))
            if not resolved.is_file():
                raise AttachmentToolError("not_found", f"file not found: {source_path}")
            mime_type = _guess_mime(resolved.name)
            created = _artifacts.create(
                resolved.name,
                mime_type,
                temp_path=str(resolved),
                conversation_id=conversation_id,
                project_id=project_id,
                created_by_tool="attachments",
                conn=conn,
            )
            artifact = created["data"]
            name = resolved.name
        else:
            artifact_row = _artifacts._fetch_artifact(conn, _require_text(artifact_id, "artifact_id"))
            artifact = _artifacts._row_to_dict(artifact_row)
            name = artifact["name"]

        attachment_id = _new_id("att")
        conn.execute(
            "INSERT INTO attachments (id, conversation_id, project_id, artifact_id, name,"
            " mime_type, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attachment_id,
                conversation_id,
                project_id,
                artifact["artifact_id"],
                name,
                artifact["mime_type"],
                artifact["size_bytes"],
                _now(),
            ),
        )
        conn.commit()
        return _ok(
            f"attached: {name} ({artifact['mime_type']})",
            {
                "attachment_id": attachment_id,
                "artifact_id": artifact["artifact_id"],
                "name": name,
                "mime_type": artifact["mime_type"],
                "size_bytes": artifact["size_bytes"],
                "conversation_id": conversation_id,
                "project_id": project_id,
            },
        )
    except _artifacts.ArtifactToolError as exc:
        conn.rollback()
        raise AttachmentToolError(exc.code, str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_rows(
    conversation_id: str | None = None,
    project_id: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List attachments, newest first, keyset-paginated; see list_attachments."""
    limit = _normalize_limit(limit)
    conn = _connect()
    try:
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if cursor:
            try:
                cursor_created, cursor_id = cursor.split("|", 1)
            except ValueError as exc:
                raise AttachmentToolError("validation_error", "malformed cursor") from exc
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created, cursor_created, cursor_id])
        rows = conn.execute(
            f"SELECT * FROM attachments WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        next_cursor = None
        if len(rows) == limit:
            last = rows[-1]
            next_cursor = f"{last['created_at']}|{last['id']}"
        return _ok(
            f"{len(rows)} attachment(s)",
            {
                "attachments": [_attachment_dict(row) for row in rows],
                "count": len(rows),
                "next_cursor": next_cursor,
            },
        )
    finally:
        conn.close()


def metadata(attachment_id: str) -> dict[str, Any]:
    """Attachment row joined with its artifact metadata; see get_attachment_metadata."""
    conn = _connect()
    try:
        row = _fetch_attachment(conn, attachment_id)
        data = _attachment_dict(row)
        data["artifact"] = None
        if row["artifact_id"] is not None:
            artifact_row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (row["artifact_id"],)
            ).fetchone()
            if artifact_row is not None:
                data["artifact"] = _artifacts._row_to_dict(artifact_row)
        return _ok(f"attachment {row['id']}: {row['name']}", data)
    finally:
        conn.close()


def read(
    attachment_id: str,
    page: int | None = None,
    offset: int = 0,
    length: int | None = None,
) -> dict[str, Any]:
    """Read an attachment's text by 200-line page or character window."""
    conn = _connect()
    try:
        row = _fetch_attachment(conn, attachment_id)
        text = _read_attachment_text(conn, row)
        data = _window_text(text, row["name"], page, offset, length)
        data.update(
            {
                "attachment_id": row["id"],
                "artifact_id": row["artifact_id"],
                "name": row["name"],
                "mime_type": row["mime_type"],
            }
        )
        if data["mode"] == "page":
            summary = f"attachment {row['id']}: page {data['page']} of {data['total_pages']}"
        else:
            summary = (
                f"attachment {row['id']}: characters {data['offset']}-"
                f"{data['offset'] + data['length']} of {data['total_chars']}"
            )
        return _ok(summary, data)
    finally:
        conn.close()


def search(
    query: str,
    conversation_id: str | None = None,
    project_id: str | None = None,
    file_types: Any = None,
    search_mode: str = "keyword",
    limit: int = 10,
) -> dict[str, Any]:
    """Bounded keyword scan over text attachments' content; see search_attachments."""
    query = _require_text(query, "query")
    if search_mode != "keyword":
        raise AttachmentToolError(
            "validation_error", "only search_mode='keyword' is supported (no FTS index exists)"
        )
    limit = _normalize_limit(limit, 50)
    if file_types is None:
        file_types = []
    elif isinstance(file_types, str):
        file_types = [file_types]
    if not isinstance(file_types, list) or any(not isinstance(t, str) for t in file_types):
        raise AttachmentToolError("validation_error", "file_types must be a list of strings")
    type_tokens = [t.strip().lower().lstrip(".") for t in file_types if t.strip()]

    conn = _connect()
    try:
        clauses = ["a.deleted_at IS NULL", "a.artifact_id IS NOT NULL"]
        params: list[Any] = []
        if conversation_id:
            clauses.append("a.conversation_id = ?")
            params.append(conversation_id)
        if project_id:
            clauses.append("a.project_id = ?")
            params.append(project_id)
        rows = conn.execute(
            "SELECT a.*, ar.deleted_at AS artifact_deleted_at FROM attachments a"
            " LEFT JOIN artifacts ar ON ar.id = a.artifact_id"
            f" WHERE {' AND '.join(clauses)} ORDER BY a.created_at DESC LIMIT 200",
            params,
        ).fetchall()

        needle = query.lower()
        hits: list[dict[str, Any]] = []
        scanned = 0
        skipped: list[dict[str, Any]] = []
        for row in rows:
            if len(hits) >= limit:
                break
            if row["artifact_deleted_at"] is not None:
                continue
            if type_tokens and not any(
                (row["mime_type"] or "").lower().find(token) >= 0
                or (row["name"] or "").lower().endswith(f".{token}")
                for token in type_tokens
            ):
                continue
            try:
                _check_supported(row)
            except AttachmentToolError:
                skipped.append({"attachment_id": row["id"], "name": row["name"],
                                "reason": "unsupported_content"})
                continue
            artifact_row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (row["artifact_id"],)
            ).fetchone()
            try:
                text = _artifacts._read_text(artifact_row)[:SEARCH_SCAN_BYTES]
            except _artifacts.ArtifactToolError:
                skipped.append({"attachment_id": row["id"], "name": row["name"],
                                "reason": "unreadable"})
                continue
            scanned += 1
            lowered = text.lower()
            start = 0
            while len(hits) < limit:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                context_start = max(0, index - SEARCH_CONTEXT_CHARS)
                context_end = min(len(text), index + len(query) + SEARCH_CONTEXT_CHARS)
                hits.append(
                    {
                        "attachment_id": row["id"],
                        "artifact_id": row["artifact_id"],
                        "name": row["name"],
                        "mime_type": row["mime_type"],
                        "char_offset": index,
                        "match": text[index : index + len(query)],
                        "context": text[context_start:context_end],
                        "truncated": len(text) == SEARCH_SCAN_BYTES
                        and (artifact_row["size_bytes"] or 0) > SEARCH_SCAN_BYTES,
                    }
                )
                start = index + len(needle)
        return _ok(
            f"{len(hits)} match(es) across {scanned} attachment(s)",
            {
                "hits": hits,
                "count": len(hits),
                "query": query,
                "search_mode": "keyword",
                "attachments_scanned": scanned,
                "attachments_skipped": skipped,
            },
        )
    finally:
        conn.close()


def remove(attachment_id: str) -> dict[str, Any]:
    """Soft-delete the attachment link; the underlying artifact is kept."""
    conn = _connect()
    try:
        row = _fetch_attachment(conn, attachment_id)
        conn.execute(
            "UPDATE attachments SET deleted_at = ? WHERE id = ?", (_now(), row["id"])
        )
        conn.commit()
        return _ok(
            f"attachment {row['id']} removed (artifact kept)",
            {"attachment_id": row["id"], "removed": True, "artifact_kept": True},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rename(attachment_id: str, name: str) -> dict[str, Any]:
    """Rename the attachment link (the artifact's own name is unchanged)."""
    name = _require_text(name, "name")
    conn = _connect()
    try:
        row = _fetch_attachment(conn, attachment_id)
        conn.execute("UPDATE attachments SET name = ? WHERE id = ?", (name, row["id"]))
        conn.commit()
        return _ok(
            f"attachment {row['id']} renamed: {row['name']} -> {name}",
            {"attachment_id": row["id"], "name": name, "previous_name": row["name"]},
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
    except AttachmentToolError as exc:
        return {
            "ok": False,
            "summary": str(exc),
            "data": {},
            "error": {"code": exc.code, "message": str(exc)},
        }
    except _paths.PathEscapeError as exc:
        return {
            "ok": False,
            "summary": str(exc),
            "data": {},
            "error": {"code": "invalid_path", "message": str(exc)},
        }
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "summary": f"database error: {exc}",
            "data": {},
            "error": {"code": "db_error", "message": str(exc)},
        }


@tool
def attach_file(
    conversation_id: str | None = None,
    source_path: str | None = None,
    artifact_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Attach a file to a conversation (imported via the artifact store).

    Provide exactly one of `source_path` (a file under the allowed roots —
    imported as a new artifact, then linked) or `artifact_id` (link an
    existing artifact). conversation_id defaults from the active run context;
    project_id is resolved from the conversation when omitted.

    Args:
        conversation_id: Owning conversation (defaults from run context).
        source_path: File to import and attach.
        artifact_id: Existing artifact to link instead of importing a file.
        project_id: Owning project (resolved from the conversation when omitted).

    Returns:
        `{ok, summary, data: {attachment_id, artifact_id, name, mime_type,
        size_bytes, conversation_id, project_id}, error}`.
    """
    return _wrap(attach, conversation_id, source_path, artifact_id, project_id)


@tool
def list_attachments(
    conversation_id: str | None = None,
    project_id: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List attachments, newest first, with keyset pagination.

    Soft-deleted attachments are excluded. Pass `next_cursor` from the
    previous page as `cursor` to continue.

    Args:
        conversation_id: Only attachments in this conversation.
        project_id: Only attachments in this project.
        limit: Page size (1-100, default 20).
        cursor: Keyset cursor from a previous response.

    Returns:
        `{ok, summary, data: {attachments: [...], count, next_cursor}, error}`.
    """
    return _wrap(list_rows, conversation_id, project_id, limit, cursor)


@tool
def get_attachment_metadata(attachment_id: str) -> dict[str, Any]:
    """Attachment row joined with its artifact metadata (hash, storage path, ...).

    Args:
        attachment_id: The attachment id from attach_file or list_attachments.

    Returns:
        `{ok, summary, data: {attachment fields..., artifact: {...}}, error}`.
    """
    return _wrap(metadata, attachment_id)


@tool
def read_attachment(
    attachment_id: str,
    page: int | None = None,
    offset: int = 0,
    length: int | None = None,
) -> dict[str, Any]:
    """Read a text attachment by fixed 200-line page or character window.

    Supports text/markdown/code/JSON/CSV (any UTF-8 text). PDF/DOCX/XLSX and
    images return a clear unsupported error — parsing them is a planned stage.
    Omit `page` to use character offsets (offset/length, like read_artifact).

    Args:
        attachment_id: Attachment to read.
        page: 1-based page number (200 lines per page); takes precedence.
        offset: Character offset when page is omitted (default 0).
        length: Characters to return (default 8000, max 40000).

    Returns:
        `{ok, summary, data: {content, mode, page|offset/length, total_chars,
        truncated_before, truncated_after, ...}, error}`.
    """
    return _wrap(read, attachment_id, page, offset, length)


@tool
def search_attachments(
    query: str,
    conversation_id: str | None = None,
    project_id: str | None = None,
    file_types: list[str] | None = None,
    search_mode: str = "keyword",
    limit: int = 10,
) -> dict[str, Any]:
    """Keyword-search the content of text attachments, with match context.

    Bounded scan (newest 200 attachments, first 512 KB of each); unsupported
    binary formats are skipped and reported. Only 'keyword' mode exists — no
    FTS index for attachment content is available.

    Args:
        query: Case-insensitive keyword to find.
        conversation_id: Restrict to this conversation.
        project_id: Restrict to this project.
        file_types: Only these types — extensions ("md", "py") or MIME tokens
            ("json", "text").
        search_mode: Must be "keyword".
        limit: Maximum matches (1-50, default 10).

    Returns:
        `{ok, summary, data: {hits: [{attachment_id, name, char_offset,
        match, context, truncated}], count, attachments_scanned,
        attachments_skipped}, error}`.
    """
    return _wrap(search, query, conversation_id, project_id, file_types, search_mode, limit)


@tool
def open_attachment_chunk(
    attachment_id: str,
    page: int | None = None,
    offset: int = 0,
    length: int | None = None,
) -> dict[str, Any]:
    """Read a chunk of an attachment — same semantics as read_attachment.

    Kept as a separate entry point for hosts/UIs that distinguish "open a
    chunk" from "read"; identical pagination rules (200-line pages or
    character windows).

    Args:
        attachment_id: Attachment to read.
        page: 1-based page number (200 lines per page); takes precedence.
        offset: Character offset when page is omitted (default 0).
        length: Characters to return (default 8000, max 40000).

    Returns:
        Same shape as read_attachment.
    """
    return _wrap(read, attachment_id, page, offset, length)


@tool
def remove_attachment(attachment_id: str) -> dict[str, Any]:
    """Detach an attachment (soft delete of the link; the artifact is kept).

    Args:
        attachment_id: Attachment to remove.

    Returns:
        `{ok, summary, data: {attachment_id, removed, artifact_kept}, error}`.
    """
    return _wrap(remove, attachment_id)


@tool
def rename_attachment(attachment_id: str, name: str) -> dict[str, Any]:
    """Rename an attachment link (the underlying artifact name is unchanged).

    Args:
        attachment_id: Attachment to rename.
        name: New display name.

    Returns:
        `{ok, summary, data: {attachment_id, name, previous_name}, error}`.
    """
    return _wrap(rename, attachment_id, name)


TOOL = [
    attach_file,
    list_attachments,
    get_attachment_metadata,
    read_attachment,
    search_attachments,
    open_attachment_chunk,
    remove_attachment,
    rename_attachment,
]
