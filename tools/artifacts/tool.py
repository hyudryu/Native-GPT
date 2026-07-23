"""Artifact Store Strands tools — durable blobs with host-compatible metadata.

Multi-tool folder: `TOOL` is a list of Strands tools. Metadata lives in the
`artifacts` table (migration 0011, opened through `tools/_lib/db.py`); blob
bytes live in a content-addressed store under the app data dir:

    <data_dir>/artifacts/
      blobs/<sha256[:2]>/<sha256>   # content-addressed, deduplicated
      tmp/                          # staging for atomic blob writes

`<data_dir>` is `$AGENTGPT_DATA_DIR` when set, else `<repo>/app-data/`
(mirroring `_lib/db.py`). `storage_path` in the artifacts table is stored
RELATIVE to the data dir (`artifacts/blobs/<shard>/<sha256>`), matching the
host's convention — the knowledge tool resolves relative storage paths
against the data dir the same way. Storing by content hash means identical
content lands on the same blob and is never written twice; per-artifact caps
(50 MB) keep the store bounded.

Soft delete: `delete_artifact` sets `deleted_at` and KEEPS the blob (other
artifacts may share the same content-addressed blob, and a future
restore/purge stage can decide retention). render_artifact /
download_artifact are intentionally NOT implemented — they need the UI and
renderer layers; this tool family is the storage + metadata foundation only.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import os
import re
import shutil
import sqlite3
import tempfile
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

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MB per artifact
BINARY_RANGE_MAX_BYTES = 1024 * 1024  # 1 MB cap per binary range read
PREVIEW_BYTES = 2048  # ~2 KB text preview
DEFAULT_READ_CHARS = 8000
MAX_READ_CHARS = 40000
HASH_CHUNK_BYTES = 64 * 1024
LIST_LIMIT_MAX = 100

# RFC 6838 token characters, type/subtype only (no parameters).
MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

# MIME families treated as text for read/preview even without a text/ prefix.
TEXT_APPLICATION_MIMES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/x-ndjson",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/sql",
    "application/graphql",
    "application/x-sh",
    "image/svg+xml",
}


class ArtifactToolError(ValueError):
    """Any artifact-tool failure; `code` becomes the result's error code."""

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
        raise ArtifactToolError("validation_error", f"{field} must be a non-empty string")
    return value.strip()


def _connect() -> sqlite3.Connection:
    try:
        return _db.connect()
    except FileNotFoundError as exc:
        raise ArtifactToolError("db_unavailable", str(exc)) from exc


def _normalize_limit(limit: Any, maximum: int = LIST_LIMIT_MAX) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ArtifactToolError("validation_error", "limit must be an integer") from exc
    if limit < 1 or limit > maximum:
        raise ArtifactToolError(
            "validation_error", f"limit must be between 1 and {maximum}"
        )
    return limit


def _normalize_offset(offset: Any) -> int:
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError) as exc:
        raise ArtifactToolError("validation_error", "offset must be a non-negative integer") from exc
    if offset < 0:
        raise ArtifactToolError("validation_error", "offset must be a non-negative integer")
    return offset


def _validate_mime(mime_type: Any) -> str:
    mime_type = _require_text(mime_type, "mime_type")
    if not MIME_RE.fullmatch(mime_type):
        raise ArtifactToolError(
            "validation_error",
            f"mime_type must be 'type/subtype' (e.g. text/markdown), got {mime_type!r}",
        )
    return mime_type.lower()


def _is_text_mime(mime_type: str | None) -> bool:
    if not mime_type:
        return False
    mime = mime_type.split(";")[0].strip().lower()
    if mime.startswith("text/"):
        return True
    if mime in TEXT_APPLICATION_MIMES:
        return True
    return mime.endswith("+json") or mime.endswith("+xml")


# ── blob store ───────────────────────────────────────────────────────────────


def _artifacts_root() -> Path:
    """Blob store root: `$AGENTGPT_DATA_DIR/artifacts/` else `<repo>/app-data/artifacts/`."""
    return _db.db_path().parent / "artifacts"


def _blob_relpath(sha256: str) -> str:
    """Storage path stored in the DB — relative to the data dir (host convention)."""
    return f"artifacts/blobs/{sha256[:2]}/{sha256}"


def _blob_abspath(sha256: str) -> Path:
    return _artifacts_root() / "blobs" / sha256[:2] / sha256


def _resolve_storage(row: sqlite3.Row) -> Path:
    """Absolute blob path for an artifacts row (relative paths resolve on the data dir)."""
    storage_path = Path(row["storage_path"])
    if not storage_path.is_absolute():
        storage_path = _db.db_path().parent / storage_path
    return storage_path


def _store_blob(stream: Any) -> tuple[str, int]:
    """Stream bytes into the content-addressed store. Returns (sha256, size_bytes).

    Stages in `tmp/` and atomically moves into `blobs/<shard>/<sha256>`; an
    existing blob (identical content) is reused. Raises content_too_large past
    the per-artifact cap.
    """
    root = _artifacts_root()
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(prefix=".blob-", dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(HASH_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise ArtifactToolError(
                        "content_too_large",
                        f"artifact exceeds the {MAX_ARTIFACT_BYTES // (1024 * 1024)} MB size cap",
                    )
                hasher.update(chunk)
                out.write(chunk)
        sha256 = hasher.hexdigest()
        target = _blob_abspath(sha256)
        if target.is_file():
            os.unlink(tmp_name)  # identical content already stored
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_name, target)
        return sha256, size
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artifact_id": row["id"],
        "name": row["name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "storage_path": row["storage_path"],
        "conversation_id": row["conversation_id"],
        "project_id": row["project_id"],
        "created_by_tool": row["created_by_tool"],
        "retention_policy": row["retention_policy"],
        "preview_path": row["preview_path"],
        "source_artifact_id": row["source_artifact_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
    }


def _fetch_artifact(
    conn: sqlite3.Connection, artifact_id: str, *, include_deleted: bool = False
) -> sqlite3.Row:
    artifact_id = _require_text(artifact_id, "artifact_id")
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None or (row["deleted_at"] is not None and not include_deleted):
        raise ArtifactToolError("not_found", f"artifact not found: {artifact_id}")
    return row


def _blob_path_or_raise(row: sqlite3.Row) -> Path:
    path = _resolve_storage(row)
    if not path.is_file():
        raise ArtifactToolError(
            "artifact_unavailable",
            f"blob for artifact {row['id']} is missing from the store ({row['storage_path']})",
        )
    return path


def _read_text(row: sqlite3.Row) -> str:
    """Decode an artifact as UTF-8 text; refuse binary with a pointer to range reads."""
    path = _blob_path_or_raise(row)
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactToolError(
            "binary_content",
            f"artifact {row['id']} ({row['mime_type'] or 'unknown type'}) is not UTF-8 text; "
            "use read_artifact_binary_range for base64 windowed access",
        ) from exc


# ── plain implementations (imported by the attachments tools) ───────────────


def create(
    name: str,
    mime_type: str,
    content: str | None = None,
    temp_path: str | None = None,
    conversation_id: str | None = None,
    project_id: str | None = None,
    created_by_tool: str | None = None,
    source_artifact_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create an artifact from text content or a file; see create_artifact."""
    name = _require_text(name, "name")
    mime_type = _validate_mime(mime_type)
    if (content is None) == (temp_path is None):
        raise ArtifactToolError(
            "validation_error", "provide exactly one of content (text) or temp_path (file)"
        )

    if content is not None:
        if not isinstance(content, str):
            raise ArtifactToolError("validation_error", "content must be a string")
        stream: Any = io.BytesIO(content.encode("utf-8"))
        close_stream = True
    else:
        resolved = _paths.resolve_under_root(_require_text(temp_path, "temp_path"))
        if not resolved.is_file():
            raise ArtifactToolError("not_found", f"temp_path file not found: {temp_path}")
        if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactToolError(
                "content_too_large",
                f"temp_path exceeds the {MAX_ARTIFACT_BYTES // (1024 * 1024)} MB size cap",
            )
        stream = resolved.open("rb")
        close_stream = True

    try:
        sha256, size = _store_blob(stream)
    finally:
        if close_stream:
            stream.close()

    own_conn = conn is None
    conn = conn or _connect()
    try:
        ctx = _context.get_run_context()
        conversation_id = conversation_id or ctx.get("conversation_id")
        if not project_id and conversation_id:
            project_id = _db.project_id_for_conversation(conn, conversation_id)
        artifact_id = _new_id("art")
        now = _now()
        conn.execute(
            "INSERT INTO artifacts (id, name, mime_type, size_bytes, sha256, storage_path,"
            " conversation_id, project_id, created_by_tool, created_at, updated_at,"
            " source_artifact_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                name,
                mime_type,
                size,
                sha256,
                _blob_relpath(sha256),
                conversation_id,
                project_id,
                created_by_tool,
                now,
                now,
                source_artifact_id,
            ),
        )
        if own_conn:
            conn.commit()
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        data = _row_to_dict(row)
        data["created"] = True
        return _ok(f"artifact created: {name} ({size} bytes, {mime_type})", data)
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def get(artifact_id: str) -> dict[str, Any]:
    """Metadata for one artifact (not its content); see get_artifact."""
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        return _ok(f"artifact {row['id']}: {row['name']}", _row_to_dict(row))
    finally:
        conn.close()


def list_rows(
    conversation_id: str | None = None,
    project_id: str | None = None,
    mime_type: str | None = None,
    created_by_tool: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List artifacts, newest first, keyset-paginated; see list_artifacts."""
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
        if mime_type:
            clauses.append("mime_type = ?")
            params.append(_validate_mime(mime_type))
        if created_by_tool:
            clauses.append("created_by_tool = ?")
            params.append(created_by_tool)
        if cursor:
            try:
                cursor_created, cursor_id = cursor.split("|", 1)
            except ValueError as exc:
                raise ArtifactToolError("validation_error", "malformed cursor") from exc
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([cursor_created, cursor_created, cursor_id])
        rows = conn.execute(
            f"SELECT * FROM artifacts WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        next_cursor = None
        if len(rows) == limit:
            last = rows[-1]
            next_cursor = f"{last['created_at']}|{last['id']}"
        return _ok(
            f"{len(rows)} artifact(s)",
            {
                "artifacts": [_row_to_dict(row) for row in rows],
                "count": len(rows),
                "next_cursor": next_cursor,
            },
        )
    finally:
        conn.close()


def read(artifact_id: str, offset: int = 0, length: int | None = None) -> dict[str, Any]:
    """Windowed character read of a text artifact; see read_artifact."""
    offset = _normalize_offset(offset)
    if length is None:
        length = DEFAULT_READ_CHARS
    else:
        try:
            length = int(length)
        except (TypeError, ValueError) as exc:
            raise ArtifactToolError("validation_error", "length must be an integer") from exc
        if length < 1 or length > MAX_READ_CHARS:
            raise ArtifactToolError(
                "validation_error", f"length must be between 1 and {MAX_READ_CHARS} characters"
            )
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        text = _read_text(row)
        total = len(text)
        window = text[offset : offset + length]
        return _ok(
            f"artifact {row['id']}: characters {offset}-{offset + len(window)} of {total}",
            {
                "artifact_id": row["id"],
                "name": row["name"],
                "mime_type": row["mime_type"],
                "content": window,
                "offset": offset,
                "length": len(window),
                "total_chars": total,
                "truncated_before": offset > 0,
                "truncated_after": offset + len(window) < total,
            },
        )
    finally:
        conn.close()


def read_binary_range(
    artifact_id: str, offset: int = 0, length: int | None = None
) -> dict[str, Any]:
    """Base64 byte-range read for any artifact (binary or text); capped at 1 MB."""
    offset = _normalize_offset(offset)
    length = BINARY_RANGE_MAX_BYTES if length is None else length
    try:
        length = int(length)
    except (TypeError, ValueError) as exc:
        raise ArtifactToolError("validation_error", "length must be an integer") from exc
    if length < 1 or length > BINARY_RANGE_MAX_BYTES:
        raise ArtifactToolError(
            "validation_error",
            f"length must be between 1 and {BINARY_RANGE_MAX_BYTES} bytes (1 MB cap)",
        )
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        path = _blob_path_or_raise(row)
        size = path.stat().st_size
        with path.open("rb") as fp:
            fp.seek(offset)
            chunk = fp.read(length)
        return _ok(
            f"artifact {row['id']}: bytes {offset}-{offset + len(chunk)} of {size} (base64)",
            {
                "artifact_id": row["id"],
                "name": row["name"],
                "mime_type": row["mime_type"],
                "content_base64": base64.b64encode(chunk).decode("ascii"),
                "offset": offset,
                "length": len(chunk),
                "size_bytes": size,
                "truncated_after": offset + len(chunk) < size,
            },
        )
    finally:
        conn.close()


def rename(artifact_id: str, name: str) -> dict[str, Any]:
    """Rename an artifact (metadata only; the blob is untouched)."""
    name = _require_text(name, "name")
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        conn.execute(
            "UPDATE artifacts SET name = ?, updated_at = ? WHERE id = ?",
            (name, _now(), row["id"]),
        )
        conn.commit()
        return _ok(
            f"artifact {row['id']} renamed: {row['name']} -> {name}",
            {"artifact_id": row["id"], "name": name, "previous_name": row["name"]},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def soft_delete(artifact_id: str) -> dict[str, Any]:
    """Soft-delete an artifact row; the blob is kept (see module docstring)."""
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        conn.execute(
            "UPDATE artifacts SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), row["id"]),
        )
        conn.commit()
        return _ok(
            f"artifact {row['id']} soft-deleted (blob retained)",
            {"artifact_id": row["id"], "deleted": True, "blob_retained": True},
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def preview(artifact_id: str) -> dict[str, Any]:
    """Small text preview (~2 KB) for text artifacts; metadata-only for binary."""
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        path = _blob_path_or_raise(row)
        raw = path.read_bytes()[: PREVIEW_BYTES + 1]
        try:
            text = raw[:PREVIEW_BYTES].decode("utf-8")
        except UnicodeDecodeError:
            return _ok(
                f"artifact {row['id']}: binary, no text preview",
                {
                    "artifact_id": row["id"],
                    "name": row["name"],
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "binary": True,
                    "preview": None,
                },
            )
        truncated = len(raw) > PREVIEW_BYTES or (row["size_bytes"] or 0) > PREVIEW_BYTES
        if truncated:
            text += "…"
        return _ok(
            f"artifact {row['id']}: {len(text)}-character preview",
            {
                "artifact_id": row["id"],
                "name": row["name"],
                "mime_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "binary": False,
                "preview": text,
                "truncated": truncated,
            },
        )
    finally:
        conn.close()


def metadata(artifact_id: str) -> dict[str, Any]:
    """Full artifacts row plus storage info (blob presence, store root)."""
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id, include_deleted=True)
        blob = _resolve_storage(row)
        data = _row_to_dict(row)
        data["storage"] = {
            "absolute_path": str(blob),
            "blob_exists": blob.is_file(),
            "artifacts_root": str(_artifacts_root()),
        }
        return _ok(f"artifact {row['id']} metadata", data)
    finally:
        conn.close()


def copy_to_project(artifact_id: str, destination: str) -> dict[str, Any]:
    """Copy an artifact's blob to a repo-relative destination path."""
    destination = _require_text(destination, "destination")
    conn = _connect()
    try:
        row = _fetch_artifact(conn, artifact_id)
        source = _blob_path_or_raise(row)
    finally:
        conn.close()
    try:
        target = _paths.resolve_under_root(destination)
    except _paths.PathEscapeError as exc:
        raise ArtifactToolError("invalid_path", str(exc)) from exc
    if target.is_dir():
        raise ArtifactToolError("invalid_path", f"destination is a directory: {destination}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return _ok(
        f"artifact {row['id']} copied to {target}",
        {
            "artifact_id": row["id"],
            "destination": str(target),
            "bytes_copied": row["size_bytes"],
        },
    )


# ── Strands tool wrappers ─────────────────────────────────────────────────


def _wrap(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except ArtifactToolError as exc:
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
def create_artifact(
    name: str,
    mime_type: str,
    content: str | None = None,
    temp_path: str | None = None,
    conversation_id: str | None = None,
    project_id: str | None = None,
    created_by_tool: str | None = None,
) -> dict[str, Any]:
    """Store content as a durable artifact (blob + artifacts-table row).

    Provide exactly one of `content` (text) or `temp_path` (a file under the
    allowed roots, copied in). The blob is content-addressed by sha256 under
    the app data dir; identical content is stored once. Artifacts are what the
    goal-supervisor's `artifact_exists` validator checks — create one for
    every deliverable (reports, charts, exports) the user should keep.
    conversation_id/project_id default from the active run context.

    Args:
        name: Human-readable file name (e.g. "market-report.md").
        mime_type: RFC 6838 type/subtype (e.g. text/markdown, image/png).
        content: Text content to store (mutually exclusive with temp_path).
        temp_path: Existing file to import (mutually exclusive with content).
        conversation_id: Owning conversation (defaults from run context).
        project_id: Owning project (resolved from the conversation when omitted).
        created_by_tool: Id of the tool that produced this artifact.

    Returns:
        `{ok, summary, data: {artifact_id, name, mime_type, size_bytes,
        sha256, storage_path, ...}, error}`.
    """
    return _wrap(
        create, name, mime_type, content, temp_path, conversation_id, project_id, created_by_tool
    )


@tool
def get_artifact(artifact_id: str) -> dict[str, Any]:
    """Fetch metadata for one artifact (name, type, size, hash — not content).

    Args:
        artifact_id: The artifact id from create_artifact or list_artifacts.

    Returns:
        `{ok, summary, data: {artifact fields...}, error}`.
    """
    return _wrap(get, artifact_id)


@tool
def list_artifacts(
    conversation_id: str | None = None,
    project_id: str | None = None,
    mime_type: str | None = None,
    created_by_tool: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List artifacts, newest first, with keyset pagination.

    Soft-deleted artifacts are excluded. Pass `next_cursor` from the previous
    page as `cursor` to continue.

    Args:
        conversation_id: Only artifacts in this conversation.
        project_id: Only artifacts in this project.
        mime_type: Only this exact type (e.g. text/markdown).
        created_by_tool: Only artifacts produced by this tool id.
        limit: Page size (1-100, default 20).
        cursor: Keyset cursor from a previous response.

    Returns:
        `{ok, summary, data: {artifacts: [...], count, next_cursor}, error}`.
    """
    return _wrap(list_rows, conversation_id, project_id, mime_type, created_by_tool, limit, cursor)


@tool
def read_artifact(
    artifact_id: str, offset: int = 0, length: int | None = None
) -> dict[str, Any]:
    """Read a window of a TEXT artifact's content (character offsets).

    Returns the requested window plus truncation flags; page through large
    files by advancing `offset`. Binary artifacts are refused — use
    read_artifact_binary_range for those.

    Args:
        artifact_id: Artifact to read.
        offset: Character offset to start at (default 0).
        length: Characters to return (default 8000, max 40000).

    Returns:
        `{ok, summary, data: {content, offset, length, total_chars,
        truncated_before, truncated_after, ...}, error}`.
    """
    return _wrap(read, artifact_id, offset, length)


@tool
def read_artifact_binary_range(
    artifact_id: str, offset: int = 0, length: int | None = None
) -> dict[str, Any]:
    """Read a byte range of any artifact as base64 (1 MB cap per call).

    Use for binary artifacts (images, PDFs, archives) or when byte-exact
    access matters; for UTF-8 text prefer read_artifact.

    Args:
        artifact_id: Artifact to read.
        offset: Byte offset to start at (default 0).
        length: Bytes to return (default and max 1048576 = 1 MB).

    Returns:
        `{ok, summary, data: {content_base64, offset, length, size_bytes,
        truncated_after, ...}, error}`.
    """
    return _wrap(read_binary_range, artifact_id, offset, length)


@tool
def rename_artifact(artifact_id: str, name: str) -> dict[str, Any]:
    """Rename an artifact (metadata only; blob content is untouched).

    Args:
        artifact_id: Artifact to rename.
        name: New human-readable name.

    Returns:
        `{ok, summary, data: {artifact_id, name, previous_name}, error}`.
    """
    return _wrap(rename, artifact_id, name)


@tool
def delete_artifact(artifact_id: str) -> dict[str, Any]:
    """Soft-delete an artifact (deleted_at set; excluded from lists/reads).

    The blob is RETAINED for now — content-addressed blobs may be shared by
    other artifacts, and a later retention stage will purge unreferenced
    blobs. Deletion is currently irreversible through the tools.

    Args:
        artifact_id: Artifact to delete.

    Returns:
        `{ok, summary, data: {artifact_id, deleted, blob_retained}, error}`.
    """
    return _wrap(soft_delete, artifact_id)


@tool
def get_artifact_preview(artifact_id: str) -> dict[str, Any]:
    """Small preview of an artifact: first ~2 KB of text, or metadata-only.

    Binary artifacts return `binary: true` with a null preview instead of
    content.

    Args:
        artifact_id: Artifact to preview.

    Returns:
        `{ok, summary, data: {preview, binary, truncated, mime_type,
        size_bytes}, error}`.
    """
    return _wrap(preview, artifact_id)


@tool
def get_artifact_metadata(artifact_id: str) -> dict[str, Any]:
    """Full artifacts-table row plus storage info (blob presence, store root).

    Includes soft-deleted artifacts (deleted_at is set on them) so callers
    can audit storage state.

    Args:
        artifact_id: Artifact to inspect.

    Returns:
        `{ok, summary, data: {artifact fields..., storage: {absolute_path,
        blob_exists, artifacts_root}}, error}`.
    """
    return _wrap(metadata, artifact_id)


@tool
def copy_artifact_to_project(artifact_id: str, destination: str) -> dict[str, Any]:
    """Copy an artifact's blob to a repo-relative destination path.

    The destination is confined to the allowed roots (repo root by default);
    parent directories are created. The artifact row is unchanged — this is a
    materialization step for deliverables the user wants in the workspace.

    Args:
        artifact_id: Artifact whose blob to copy.
        destination: Repo-relative target path (e.g. "reports/out.md").

    Returns:
        `{ok, summary, data: {artifact_id, destination, bytes_copied}, error}`.
    """
    return _wrap(copy_to_project, artifact_id, destination)


TOOL = [
    create_artifact,
    get_artifact,
    list_artifacts,
    read_artifact,
    read_artifact_binary_range,
    rename_artifact,
    delete_artifact,
    get_artifact_preview,
    get_artifact_metadata,
    copy_artifact_to_project,
]
