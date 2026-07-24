"""File System Extensions Strands tools — stat/copy/trash/binary/archive ops.

Multi-tool folder: `TOOL` is a list of Strands tools. Every filesystem path
goes through `tools/_lib/paths.py::resolve_under_root` (allowed-root
confinement); the lib is loaded by file path like the other tools.

Trash store design (documented deviation from the product plan)
---------------------------------------------------------------
The plan listed a `trash_records` table, but migration 0011 does NOT create
one and this stage may not add migrations. Instead, trash is a file store
under the app data dir:

    <data_dir>/trash/
      manifest.json          # {"records": [ {trash_id, original_path, ...} ]}
      blobs/<trash_id>       # the moved file's bytes

`<data_dir>` is `$AGENTGPT_DATA_DIR` when set, else `<repo>/app-data/` —
mirroring `tools/_lib/db.py`'s path resolution. `trash_file` moves the file
into `blobs/` and appends a manifest record; restore/permanent-delete/list
operate on that store. Manifest writes are atomic (tmp + os.replace).

Archive extraction is zip-slip guarded: members with absolute paths, drive
letters, or `..` segments that would escape the destination are rejected
before anything is written.

Deviation from the plan: `extract_archive` / `list_archive_contents` take a
filesystem `archive_path` (resolved under the allowed roots) instead of an
artifact_id — the artifacts tool stage doesn't exist yet; an artifact wrapper
can be added later.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from strands import tool

# Load the shared `_lib/paths.py` as a module (no package context when the
# runtime imports this file standalone).
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
resolve_under_root = _paths.resolve_under_root
PathEscapeError = _paths.PathEscapeError

STAT_HASH_MAX_BYTES = 16 * 1024 * 1024  # stat_file hashes files < 16 MB
READ_BINARY_MAX_BYTES = 1024 * 1024  # 1 MB cap per read_binary_range call
ARCHIVE_MEMBER_MAX_BYTES = 256 * 1024 * 1024  # per-member cap on extraction
HASH_CHUNK_BYTES = 64 * 1024


class FsToolError(ValueError):
    """Any fs-extensions failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _result(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _failure(
    code: str, summary: str, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "summary": summary,
        "data": data or {},
        "error": {"code": code, "message": message},
    }


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(HASH_CHUNK_BYTES):
            hasher.update(chunk)
    return hasher.hexdigest()


def _require_path(value: Any, name: str = "path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise FsToolError("invalid_path", f"{name} must be a non-empty string")
    return value


def _verify_sha256(resolved: Path, expected: str | None, label: str) -> str | None:
    """Return the file's sha256; raise hash_mismatch when it differs from `expected`."""
    if not resolved.is_file():
        raise FsToolError("not_found", f"file not found: {label}")
    digest = _sha256_file(resolved)
    if expected is not None and digest != expected.strip().lower():
        raise FsToolError(
            "hash_mismatch",
            f"sha256 of {label} does not match expected value "
            f"(got {digest}, expected {expected})",
        )
    return digest


# ── Trash store ─────────────────────────────────────────────────────────────


def _trash_dir() -> Path:
    data_dir = os.environ.get("AGENTGPT_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).resolve() / "trash"
    return _paths.repo_root() / "app-data" / "trash"


def _manifest_path() -> Path:
    return _trash_dir() / "manifest.json"


def _load_manifest() -> dict[str, Any]:
    path = _manifest_path()
    if not path.is_file():
        return {"records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"records": []}
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return {"records": []}
    return data


def _save_manifest(manifest: dict[str, Any]) -> None:
    trash = _trash_dir()
    trash.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".trash-manifest-", dir=str(trash))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2)
        os.replace(tmp_path, _manifest_path())
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _find_record(manifest: dict[str, Any], trash_id: str) -> dict[str, Any] | None:
    for record in manifest["records"]:
        if record.get("trash_id") == trash_id:
            return record
    return None


def _trash_blob_path(trash_id: str) -> Path:
    # trash_id is uuid hex — safe as a filename, no traversal surface.
    return _trash_dir() / "blobs" / trash_id


# ── stat_file ───────────────────────────────────────────────────────────────


@tool
def stat_file(path: str) -> dict[str, Any]:
    """Return metadata for a file or directory under the allowed roots.

    Includes a sha256 for regular files smaller than 16 MB (hashing larger
    files is skipped to bound latency; use hash_file for those).

    Args:
        path: Path relative to the repo root, or absolute inside an allowed
            root.

    Returns:
        `{ok, summary, data: {path, exists, is_file, is_dir, size_bytes,
        modified_at, created_at, permissions, sha256?}, error}`.
    """

    try:
        path = _require_path(path)
        resolved = resolve_under_root(path)
        if not resolved.exists():
            raise FsToolError("not_found", f"path not found: {path}")
        info = resolved.stat()
        data: dict[str, Any] = {
            "path": path,
            "resolved_path": str(resolved),
            "exists": True,
            "is_file": resolved.is_file(),
            "is_dir": resolved.is_dir(),
            "size_bytes": info.st_size,
            "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            "created_at": datetime.fromtimestamp(info.st_ctime, UTC).isoformat(),
            "permissions": oct(info.st_mode & 0o777),
        }
        if resolved.is_file() and info.st_size < STAT_HASH_MAX_BYTES:
            data["sha256"] = _sha256_file(resolved)
        else:
            data["sha256"] = None
        kind = "directory" if resolved.is_dir() else "file"
        return _result(f"{kind} {path}: {info.st_size} bytes", data)
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, f"could not stat {path}", str(exc), {"path": path})
    except OSError as exc:
        return _failure("io_error", f"OS error on {path}", str(exc), {"path": path})


# ── copy_file ───────────────────────────────────────────────────────────────


@tool
def copy_file(
    source: str,
    destination: str,
    overwrite: bool = False,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Copy a file within the allowed roots, with optional hash verification.

    Args:
        source: Existing file path (relative to repo root or absolute under
            an allowed root).
        destination: Target path. Parent directories are created.
        overwrite: When false (default), an existing destination is an error
            — silent overwrites are refused.
        expected_source_sha256: When given, the source is hashed first and
            the copy aborts on mismatch (guards against copying a file that
            changed since you last saw it).

    Returns:
        `{ok, summary, data: {source, destination, bytes, sha256, overwritten},
        error}`.
    """

    try:
        source = _require_path(source, "source")
        destination = _require_path(destination, "destination")
        src = resolve_under_root(source)
        dst = resolve_under_root(destination)
        digest = _verify_sha256(src, expected_source_sha256, source)
        assert digest is not None
        if dst.exists():
            if not overwrite:
                raise FsToolError(
                    "destination_exists",
                    f"destination exists: {destination} (pass overwrite=True to replace it)",
                )
            if dst.is_dir():
                raise FsToolError("destination_is_dir", f"destination is a directory: {destination}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish: copy to a temp file in the target dir, then rename.
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".agentgpt-copy-", dir=str(dst.parent))
        try:
            with os.fdopen(tmp_fd, "wb") as out, src.open("rb") as inp:
                shutil.copyfileobj(inp, out, HASH_CHUNK_BYTES)
            shutil.copystat(src, tmp_path)
            os.replace(tmp_path, dst)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return _result(
            f"copied {source} -> {destination} ({digest[:12]}…)",
            {
                "source": source,
                "destination": destination,
                "bytes": dst.stat().st_size,
                "sha256": digest,
                "overwritten": overwrite,
            },
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "copy failed", str(exc), {"source": source, "destination": destination})
    except OSError as exc:
        return _failure("io_error", "OS error during copy", str(exc), {"source": source})


# ── trash / restore / list / permanent delete ───────────────────────────────


@tool
def trash_file(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Move a file into the app trash store (reversible).

    The file is moved to `<app data dir>/trash/blobs/<trash_id>` and a record
    (original path, size, sha256, timestamp) is added to the trash manifest.
    Use restore_trashed_file to undo, or permanently_delete_file to purge.

    Args:
        path: File to trash (under the allowed roots). Directories are
            refused — trash them with an explicit archive-then-delete flow.
        expected_sha256: Optional hash check before moving.

    Returns:
        `{ok, summary, data: {trash_id, original_path, bytes, sha256}, error}`.
    """

    try:
        path = _require_path(path)
        resolved = resolve_under_root(path)
        if resolved.is_dir():
            raise FsToolError("is_directory", f"trash_file refuses directories: {path}")
        digest = _verify_sha256(resolved, expected_sha256, path)
        trash_id = uuid.uuid4().hex
        blob = _trash_blob_path(trash_id)
        blob.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved), str(blob))
        manifest = _load_manifest()
        record = {
            "trash_id": trash_id,
            "original_path": str(resolved),
            "given_path": path,
            "size_bytes": blob.stat().st_size,
            "sha256": digest,
            "trashed_at": datetime.now(UTC).isoformat(),
        }
        manifest["records"].append(record)
        _save_manifest(manifest)
        return _result(
            f"trashed {path} (trash_id {trash_id[:8]}…)",
            {
                "trash_id": trash_id,
                "original_path": str(resolved),
                "bytes": record["size_bytes"],
                "sha256": digest,
            },
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "trash failed", str(exc), {"path": path})
    except OSError as exc:
        return _failure("io_error", "OS error during trash", str(exc), {"path": path})


@tool
def restore_trashed_file(trash_id: str, destination: str | None = None) -> dict[str, Any]:
    """Restore a trashed file to its original path (or a given destination).

    Args:
        trash_id: Id returned by trash_file / list_trashed_files.
        destination: Optional override path (under the allowed roots). When
            omitted, the file returns to its original location. Refuses to
            overwrite an existing file.

    Returns:
        `{ok, summary, data: {trash_id, restored_path, bytes}, error}`.
    """

    try:
        if not isinstance(trash_id, str) or not trash_id.strip():
            raise FsToolError("invalid_trash_id", "trash_id must be a non-empty string")
        manifest = _load_manifest()
        record = _find_record(manifest, trash_id)
        if record is None:
            raise FsToolError("not_found", f"no trash record: {trash_id}")
        blob = _trash_blob_path(trash_id)
        if not blob.is_file():
            raise FsToolError("blob_missing", f"trash blob is missing for {trash_id}")
        if destination is not None:
            target = resolve_under_root(_require_path(destination, "destination"))
        else:
            target = Path(record["original_path"])
        if target.exists():
            raise FsToolError(
                "destination_exists",
                f"restore target exists: {target} (choose another destination)",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(blob), str(target))
        manifest["records"] = [r for r in manifest["records"] if r.get("trash_id") != trash_id]
        _save_manifest(manifest)
        return _result(
            f"restored {trash_id[:8]}… -> {target}",
            {"trash_id": trash_id, "restored_path": str(target), "bytes": target.stat().st_size},
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "restore failed", str(exc), {"trash_id": trash_id})
    except OSError as exc:
        return _failure("io_error", "OS error during restore", str(exc), {"trash_id": trash_id})


@tool
def list_trashed_files(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    """List files currently in the trash store, newest first.

    Args:
        limit: Max records to return (1-100, default 20).
        cursor: Opaque offset cursor from a previous call's `next_cursor`.

    Returns:
        `{ok, summary, data: {items: [{trash_id, original_path, size_bytes,
        sha256, trashed_at}], count, next_cursor}, error}`.
    """

    try:
        limit_int = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        return _failure("invalid_limit", "invalid limit", f"limit must be an integer: {limit!r}")
    try:
        offset = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        return _failure("invalid_cursor", "invalid cursor", f"cursor must be an offset integer: {cursor!r}")
    manifest = _load_manifest()
    records = sorted(
        manifest["records"], key=lambda r: r.get("trashed_at", ""), reverse=True
    )
    window = records[offset : offset + limit_int]
    items = [
        {
            "trash_id": r.get("trash_id"),
            "original_path": r.get("original_path"),
            "size_bytes": r.get("size_bytes"),
            "sha256": r.get("sha256"),
            "trashed_at": r.get("trashed_at"),
        }
        for r in window
    ]
    next_cursor = str(offset + limit_int) if offset + limit_int < len(records) else None
    return _result(
        f"{len(items)} trashed file(s) (of {len(records)} total)",
        {"items": items, "count": len(items), "total": len(records), "next_cursor": next_cursor},
    )


@tool
def permanently_delete_file(
    path: str | None = None,
    trash_id: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Irreversibly delete a file — either a live path or a trash record.

    Exactly one of `path` / `trash_id` must be given. This is a HARD delete:
    no further recovery is possible afterwards (for `path`, trashing first
    via trash_file is the recoverable alternative).

    Args:
        path: Live file path under the allowed roots.
        trash_id: Trash record to purge (deletes the stored blob).
        expected_sha256: Optional hash check before deleting.

    Returns:
        `{ok, summary, data: {deleted, bytes_freed}, error}`.
    """

    try:
        if (path is None) == (trash_id is None):
            raise FsToolError(
                "invalid_arguments", "exactly one of path / trash_id must be provided"
            )
        if trash_id is not None:
            manifest = _load_manifest()
            record = _find_record(manifest, trash_id)
            if record is None:
                raise FsToolError("not_found", f"no trash record: {trash_id}")
            blob = _trash_blob_path(trash_id)
            size = blob.stat().st_size if blob.is_file() else 0
            if expected_sha256 is not None and blob.is_file():
                _verify_sha256(blob, expected_sha256, f"trash blob {trash_id}")
            if blob.is_file():
                blob.unlink()
            manifest["records"] = [
                r for r in manifest["records"] if r.get("trash_id") != trash_id
            ]
            _save_manifest(manifest)
            return _result(
                f"purged trash {trash_id[:8]}… ({size} bytes)",
                {"deleted": f"trash:{trash_id}", "bytes_freed": size},
            )
        assert path is not None
        path = _require_path(path)
        resolved = resolve_under_root(path)
        if resolved.is_dir():
            raise FsToolError(
                "is_directory",
                f"permanently_delete_file refuses directories: {path}",
            )
        _verify_sha256(resolved, expected_sha256, path)
        size = resolved.stat().st_size
        resolved.unlink()
        return _result(
            f"permanently deleted {path} ({size} bytes)",
            {"deleted": path, "bytes_freed": size},
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "delete failed", str(exc), {"path": path, "trash_id": trash_id})
    except OSError as exc:
        return _failure("io_error", "OS error during delete", str(exc), {"path": path})


# ── read_binary_range ───────────────────────────────────────────────────────


@tool
def read_binary_range(path: str, offset: int, length: int) -> dict[str, Any]:
    """Read a byte range of any file (binary-safe) as base64.

    Args:
        path: File under the allowed roots.
        offset: 0-based byte offset to start at.
        length: Number of bytes to read (max 1 MB per call).

    Returns:
        `{ok, summary, data: {path, offset, requested_length, returned_length,
        size_bytes, base64, truncated}, error}`.
    """

    try:
        path = _require_path(path)
        offset_int = int(offset)
        length_int = int(length)
        if offset_int < 0:
            raise FsToolError("invalid_offset", "offset must be >= 0")
        if length_int < 1 or length_int > READ_BINARY_MAX_BYTES:
            raise FsToolError(
                "invalid_length", f"length must be 1..{READ_BINARY_MAX_BYTES} bytes"
            )
        resolved = resolve_under_root(path)
        if not resolved.is_file():
            raise FsToolError("not_found", f"file not found: {path}")
        size = resolved.stat().st_size
        with resolved.open("rb") as fp:
            fp.seek(offset_int)
            payload = fp.read(length_int)
        return _result(
            f"{len(payload)} bytes from {path} at offset {offset_int}",
            {
                "path": path,
                "offset": offset_int,
                "requested_length": length_int,
                "returned_length": len(payload),
                "size_bytes": size,
                "base64": base64.b64encode(payload).decode("ascii"),
                "truncated": offset_int + len(payload) < size,
            },
        )
    except (FsToolError, PathEscapeError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else (
            "path_escape" if isinstance(exc, PathEscapeError) else "invalid_range"
        )
        return _failure(code, "binary read failed", str(exc), {"path": path})
    except OSError as exc:
        return _failure("io_error", "OS error during binary read", str(exc), {"path": path})


# ── archives ────────────────────────────────────────────────────────────────


def _validate_member_name(name: str) -> PurePosixPath:
    """Reject archive members that could escape the extraction root (zip-slip)."""
    # Normalize separators; archive formats use '/'.
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise FsToolError("unsafe_member", f"archive member has an absolute path: {name!r}")
    if any(part == ".." for part in pure.parts):
        raise FsToolError("unsafe_member", f"archive member escapes destination: {name!r}")
    if not normalized or normalized.endswith("/"):
        return pure  # directory entry
    return pure


def _safe_members_zip(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    for info in archive.infolist():
        _validate_member_name(info.filename)
        if info.file_size > ARCHIVE_MEMBER_MAX_BYTES:
            raise FsToolError(
                "member_too_large",
                f"member {info.filename!r} exceeds {ARCHIVE_MEMBER_MAX_BYTES} byte cap",
            )
        members.append(info)
    return members


def _safe_members_tar(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for info in archive.getmembers():
        _validate_member_name(info.name)
        if info.isdev() or info.issym() or info.islnk():
            raise FsToolError(
                "unsafe_member",
                f"archive member is a device/symlink/hardlink (refused): {info.name!r}",
            )
        if info.size > ARCHIVE_MEMBER_MAX_BYTES:
            raise FsToolError(
                "member_too_large",
                f"member {info.name!r} exceeds {ARCHIVE_MEMBER_MAX_BYTES} byte cap",
            )
        members.append(info)
    return members


@tool
def create_archive(
    paths: list[str],
    format: str,
    output_name: str | None = None,
) -> dict[str, Any]:
    """Create a zip or tar.gz archive from files/directories under the allowed roots.

    Args:
        paths: Files or directories to include (resolved under the allowed
            roots; escaping paths are rejected). Directories are added
            recursively. Archive member names are relative to the repo root.
        format: "zip" or "tar.gz".
        output_name: Archive file name (created under the repo root). Defaults
            to "archive-<timestamp>.<ext>".

    Returns:
        `{ok, summary, data: {archive_path, format, members, bytes}, error}`.
    """

    try:
        fmt = str(format).strip().lower()
        if fmt not in {"zip", "tar.gz", "tgz"}:
            raise FsToolError("invalid_format", f"format must be 'zip' or 'tar.gz' (got {format!r})")
        if not isinstance(paths, list) or not paths:
            raise FsToolError("invalid_paths", "paths must be a non-empty list of paths")
        resolved_inputs: list[Path] = []
        for entry in paths:
            resolved = resolve_under_root(_require_path(str(entry)))
            if not resolved.exists():
                raise FsToolError("not_found", f"path not found: {entry}")
            resolved_inputs.append(resolved)

        root = _paths.repo_root()
        ext = ".zip" if fmt == "zip" else ".tar.gz"
        if output_name is None:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            output_name = f"archive-{stamp}{ext}"
        output = resolve_under_root(_require_path(output_name, "output_name"))
        if not str(output).lower().endswith(ext):
            output = output.with_name(output.name + ext)
        if output.exists():
            raise FsToolError("destination_exists", f"archive already exists: {output.name}")
        output.parent.mkdir(parents=True, exist_ok=True)

        def arcname(p: Path) -> str:
            try:
                return p.relative_to(root).as_posix()
            except ValueError:
                return p.name

        members = 0
        if fmt == "zip":
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in resolved_inputs:
                    if item.is_dir():
                        for child in sorted(item.rglob("*")):
                            if child.is_file():
                                zf.write(child, arcname(child))
                                members += 1
                    else:
                        zf.write(item, arcname(item))
                        members += 1
        else:
            with tarfile.open(output, "w:gz") as tf:
                for item in resolved_inputs:
                    tf.add(item, arcname=arcname(item), recursive=True)
                    members += sum(1 for c in item.rglob("*") if c.is_file()) if item.is_dir() else 1
        return _result(
            f"created {output.name} with {members} member(s)",
            {
                "archive_path": str(output),
                "format": fmt,
                "members": members,
                "bytes": output.stat().st_size,
            },
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "archive creation failed", str(exc))
    except OSError as exc:
        return _failure("io_error", "OS error creating archive", str(exc))


@tool
def extract_archive(
    archive_path: str,
    destination: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Extract a zip or tar.gz archive SAFELY (zip-slip guarded).

    Members with absolute paths, drive letters, `..` traversal, symlinks,
    hardlinks, or device entries are rejected before anything is written.
    Per-member size is capped at 256 MB.

    Args:
        archive_path: Archive file under the allowed roots. (Plan deviation:
            takes a filesystem path, not an artifact_id — the artifacts stage
            doesn't exist yet.)
        destination: Directory to extract into (created if missing).
        overwrite: When false (default), any colliding existing file aborts
            the extraction.

    Returns:
        `{ok, summary, data: {archive_path, destination, extracted, skipped},
        error}`.
    """

    try:
        archive_path = _require_path(archive_path, "archive_path")
        destination = _require_path(destination, "destination")
        archive = resolve_under_root(archive_path)
        if not archive.is_file():
            raise FsToolError("not_found", f"archive not found: {archive_path}")
        dest = resolve_under_root(destination)
        dest.mkdir(parents=True, exist_ok=True)

        extracted = 0
        lower = archive.name.lower()
        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                members = _safe_members_zip(zf)
                # Collision pre-check so a partial extraction never happens
                # when overwrite=False.
                if not overwrite:
                    collisions = [
                        i.filename for i in members
                        if not i.is_dir() and (dest / i.filename).exists()
                    ]
                    if collisions:
                        raise FsToolError(
                            "destination_exists",
                            f"extraction would overwrite: {collisions[0]!r} "
                            f"(pass overwrite=True)",
                        )
                for info in members:
                    if info.is_dir():
                        (dest / info.filename).mkdir(parents=True, exist_ok=True)
                        continue
                    target = dest / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out, HASH_CHUNK_BYTES)
                    extracted += 1
        elif lower.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as tf:
                members = _safe_members_tar(tf)
                if not overwrite:
                    collisions = [
                        i.name for i in members if i.isfile() and (dest / i.name).exists()
                    ]
                    if collisions:
                        raise FsToolError(
                            "destination_exists",
                            f"extraction would overwrite: {collisions[0]!r} "
                            f"(pass overwrite=True)",
                        )
                for info in members:
                    if info.isdir():
                        (dest / info.name).mkdir(parents=True, exist_ok=True)
                        continue
                    if not info.isfile():
                        continue
                    target = dest / info.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(info)
                    if src is None:
                        continue
                    with src, target.open("wb") as out:
                        shutil.copyfileobj(src, out, HASH_CHUNK_BYTES)
                    extracted += 1
        else:
            raise FsToolError(
                "invalid_format", f"unsupported archive type: {archive.name} (zip or tar.gz)"
            )
        return _result(
            f"extracted {extracted} file(s) to {destination}",
            {
                "archive_path": archive_path,
                "destination": str(dest),
                "extracted": extracted,
            },
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "extraction failed", str(exc), {"archive_path": archive_path})
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        return _failure("invalid_archive", "corrupt or unreadable archive", str(exc))
    except OSError as exc:
        return _failure("io_error", "OS error during extraction", str(exc))


@tool
def list_archive_contents(archive_path: str) -> dict[str, Any]:
    """List the members of a zip or tar.gz archive without extracting.

    Member names are validated (unsafe entries are flagged, not followed).

    Args:
        archive_path: Archive file under the allowed roots.

    Returns:
        `{ok, summary, data: {archive_path, format, members: [{name, size,
        is_dir, safe}], count}, error}`.
    """

    try:
        archive_path = _require_path(archive_path, "archive_path")
        archive = resolve_under_root(archive_path)
        if not archive.is_file():
            raise FsToolError("not_found", f"archive not found: {archive_path}")
        members: list[dict[str, Any]] = []
        lower = archive.name.lower()
        fmt: str
        if lower.endswith(".zip"):
            fmt = "zip"
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    try:
                        _validate_member_name(info.filename)
                        safe = True
                    except FsToolError:
                        safe = False
                    members.append(
                        {
                            "name": info.filename,
                            "size": info.file_size,
                            "is_dir": info.is_dir(),
                            "safe": safe,
                        }
                    )
        elif lower.endswith((".tar.gz", ".tgz")):
            fmt = "tar.gz"
            with tarfile.open(archive, "r:gz") as tf:
                for info in tf.getmembers():
                    safe = not (info.isdev() or info.issym() or info.islnk())
                    try:
                        _validate_member_name(info.name)
                    except FsToolError:
                        safe = False
                    members.append(
                        {"name": info.name, "size": info.size, "is_dir": info.isdir(), "safe": safe}
                    )
        else:
            raise FsToolError(
                "invalid_format", f"unsupported archive type: {archive.name} (zip or tar.gz)"
            )
        return _result(
            f"{len(members)} member(s) in {archive.name}",
            {"archive_path": archive_path, "format": fmt, "members": members, "count": len(members)},
        )
    except (FsToolError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, FsToolError) else "path_escape"
        return _failure(code, "could not list archive", str(exc), {"archive_path": archive_path})
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        return _failure("invalid_archive", "corrupt or unreadable archive", str(exc))
    except OSError as exc:
        return _failure("io_error", "OS error reading archive", str(exc))


TOOL = [
    stat_file,
    copy_file,
    trash_file,
    restore_trashed_file,
    list_trashed_files,
    permanently_delete_file,
    read_binary_range,
    create_archive,
    extract_archive,
    list_archive_contents,
]
