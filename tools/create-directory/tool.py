"""Create directory Strands tool.

Idempotent directory creation under the repository root.
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


class CreateDirectoryError(ValueError):
    """Raised for any create_directory failure."""


def create(path: str) -> dict[str, Any]:
    """Create the directory at `path` (and parents). Standard-schema result dict."""
    if not isinstance(path, str) or not path.strip():
        raise CreateDirectoryError("path must be a non-empty string")

    resolved = resolve_under_root(path)
    already_existed = resolved.exists()
    if already_existed and not resolved.is_dir():
        raise CreateDirectoryError(
            f"path already exists and is not a directory: {path}"
        )
    resolved.mkdir(parents=True, exist_ok=True)

    return {
        "ok": True,
        "summary": f"{'already existed' if already_existed else 'created'} directory {path}",
        "data": {"path": path, "already_existed": already_existed},
        "error": None,
    }


@tool
def create_directory(path: str) -> dict[str, Any]:
    """Create a directory under the repository root.

    Creates any missing parent directories. Idempotent: returns success if
    the directory already exists.

    Args:
        path: Directory path relative to the repo root.

    Returns:
        `{ok, summary, data: {path, already_existed}, error}`.
    """

    try:
        return create(path)
    except (CreateDirectoryError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not create directory {path}",
            "data": {"path": path},
            "error": {"code": "mkdir_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error creating {path}",
            "data": {"path": path},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = create_directory
