"""Delete file Strands tool.

Deletes a single file under the repository root. Refuses to delete
directories (the model should use shell_execute with explicit approval for
recursive deletes — those are too destructive to do quietly).
"""

from __future__ import annotations

import importlib.util
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


class DeleteFileError(ValueError):
    """Raised for any delete_file failure."""


def delete(path: str) -> dict[str, Any]:
    """Delete the file at `path`. Standard-schema result dict."""
    if not isinstance(path, str) or not path.strip():
        raise DeleteFileError("path must be a non-empty string")

    resolved = resolve_under_root(path)
    if not resolved.exists():
        raise DeleteFileError(f"file not found: {path}")
    if not resolved.is_file():
        raise DeleteFileError(
            f"not a regular file: {path} (delete_file refuses to delete directories)"
        )

    size = resolved.stat().st_size
    resolved.unlink()

    return {
        "ok": True,
        "summary": f"deleted {path} ({size} bytes)",
        "data": {"path": path, "bytes_freed": size},
        "error": None,
    }


@tool
def delete_file(path: str) -> dict[str, Any]:
    """Delete a single file under the repository root.

    Refuses to delete directories — recursive deletes are too destructive to
    do without explicit per-call approval. Use shell_execute for those (it
    prompts the user before running).

    Args:
        path: Path relative to the repo root. Must be a file.

    Returns:
        `{ok, summary, data: {path, bytes_freed}, error}`.
    """

    try:
        return delete(path)
    except (DeleteFileError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not delete {path}",
            "data": {"path": path},
            "error": {"code": "delete_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error deleting {path}",
            "data": {"path": path},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = delete_file
