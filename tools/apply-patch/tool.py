"""Apply patch Strands tool.

Search-and-replace patch format (v1). Each patch is a list of edits; each
edit has a `find` string that must appear verbatim in the file and a `replace`
string that takes its place. The `all` flag controls whether only the first
match (default) or every match is replaced.

We deliberately do NOT support unified-diff format in v1; a real parser is
~150 LOC and the spec's "clearer diffs / easier undo" justification is
achievable with this simpler shape. Phase 2c can add unified-diff.

Atomic per file: if ANY edit fails to apply, the file is left unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from typing import Any

from strands import tool

_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
resolve_under_root = _paths.resolve_under_root
PathEscapeError = _paths.PathEscapeError

MAX_FILE_BYTES = 2 * 1024 * 1024  # refuse to patch files larger than 2 MB
MAX_EDITS = 50  # cap edit count per call


class ApplyPatchError(ValueError):
    """Raised for any apply_patch failure (find not found, IO, size cap)."""


def _normalize_edits(edits: Any) -> list[dict[str, Any]]:
    """Accept either a single edit dict or a list of edit dicts."""
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list):
        raise ApplyPatchError("patch must be a dict or a list of dicts")
    if len(edits) > MAX_EDITS:
        raise ApplyPatchError(f"too many edits (max {MAX_EDITS}, got {len(edits)})")
    normalized: list[dict[str, Any]] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ApplyPatchError(f"edit {i} must be an object")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str):
            raise ApplyPatchError(f"edit {i} is missing a string 'find'")
        if not isinstance(replace, str):
            raise ApplyPatchError(f"edit {i} is missing a string 'replace'")
        if not find:
            raise ApplyPatchError(f"edit {i} has an empty 'find' (would match everywhere)")
        replace_all = bool(edit.get("all", False))
        normalized.append({"find": find, "replace": replace, "all": replace_all})
    return normalized


def _apply_edits(content: str, edits: list[dict[str, Any]]) -> tuple[str, int]:
    """Apply edits sequentially. Returns (new_content, total_replacements).

    Raises ApplyPatchError if any edit's `find` doesn't match.
    """
    new_content = content
    total = 0
    for edit in edits:
        find = edit["find"]
        replace = edit["replace"]
        if edit["all"]:
            count = new_content.count(find)
            if count == 0:
                raise ApplyPatchError(f"find string not present: {find!r}")
            new_content = new_content.replace(find, replace)
            total += count
        else:
            occurrences = new_content.count(find)
            if occurrences == 0:
                raise ApplyPatchError(
                    f"find string not present: {find!r} "
                    f"(note: it may appear multiple times — pass all=true to replace every match)"
                )
            if occurrences > 1:
                raise ApplyPatchError(
                    f"find string appears {occurrences} times but all=false: {find!r} "
                    f"(pass all=true to replace every match, or make the find string more specific)"
                )
            new_content = new_content.replace(find, replace, 1)
            total += 1
    return new_content, total


def apply(path: str, patch: Any) -> dict[str, Any]:
    """Apply `patch` (one edit or list of edits) to `path`. Standard-schema result dict."""
    if not isinstance(path, str) or not path.strip():
        raise ApplyPatchError("path must be a non-empty string")

    edits = _normalize_edits(patch)
    resolved = resolve_under_root(path)
    if not resolved.exists():
        raise ApplyPatchError(f"file not found: {path}")
    if not resolved.is_file():
        raise ApplyPatchError(f"not a regular file: {path}")

    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ApplyPatchError(f"file exceeds {MAX_FILE_BYTES} byte cap (got {size})")

    original = resolved.read_text(encoding="utf-8", errors="replace")
    try:
        new_content, replacements = _apply_edits(original, edits)
    except ApplyPatchError:
        raise

    if new_content == original:
        return {
            "ok": True,
            "summary": f"no changes to {path}",
            "data": {"path": path, "edits_applied": 0, "replacements": 0, "bytes": size},
            "error": None,
        }

    # Atomic write (same pattern as write_file).
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".agentgpt-patch-", suffix=".tmp", dir=str(resolved.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            fp.write(new_content)
        os.replace(tmp_path, resolved)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "summary": f"applied {len(edits)} edit(s), {replacements} replacement(s) to {path}",
        "data": {
            "path": path,
            "edits_applied": len(edits),
            "replacements": replacements,
            "bytes": len(new_content.encode("utf-8")),
        },
        "error": None,
    }


@tool
def apply_patch(path: str, patch: Any) -> dict[str, Any]:
    """Apply search-and-replace edits to a file under the repository root.

    Prefer this over `write_file` for targeted changes to existing files: it
    fails cleanly if a `find` string isn't present (so the model can't silently
    no-op) and produces a cleaner audit trail.

    Each edit is `{find: str, replace: str, all?: bool}`. By default only the
    first match is replaced; pass `all=true` to replace every occurrence. The
    `find` string must be unique in the file unless `all=true`.

    Args:
        path: File to edit, relative to the repo root.
        patch: Either a single edit object or a list of edit objects. Edits
            apply sequentially in the order given.

    Returns:
        `{ok, summary, data: {path, edits_applied, replacements, bytes}, error}`.
    """

    try:
        return apply(path, patch)
    except (ApplyPatchError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not patch {path}",
            "data": {"path": path},
            "error": {"code": "patch_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error patching {path}",
            "data": {"path": path},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = apply_patch
