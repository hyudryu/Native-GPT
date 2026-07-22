"""Move file Strands tool.

Moves (renames) a file inside the repository root. Both source and
destination must resolve under an allowed root. Refuses to overwrite an
existing destination by default.
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


class MoveFileError(ValueError):
    """Raised for any move_file failure (missing source, traversal, overwrite)."""


def move(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    """Move `source` to `destination`. Standard-schema result dict."""
    for label, value in (("source", source), ("destination", destination)):
        if not isinstance(value, str) or not value.strip():
            raise MoveFileError(f"{label} must be a non-empty string")

    src = resolve_under_root(source)
    dst = resolve_under_root(destination)

    if not src.exists():
        raise MoveFileError(f"source not found: {source}")
    if not src.is_file():
        raise MoveFileError(f"source is not a regular file: {source}")
    if dst.exists() and not overwrite:
        raise MoveFileError(
            f"destination already exists: {destination} (pass overwrite=true to replace)"
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dst) if dst.exists() else src.rename(dst)
    except OSError as exc:
        # Cross-device rename can fail; fall back to copy+delete.
        if exc.errno == 18:  # EXDEV — cross-device link not permitted
            import shutil

            shutil.copy2(src, dst)
            src.unlink()
        else:
            raise

    return {
        "ok": True,
        "summary": f"moved {source} -> {destination}",
        "data": {
            "source": source,
            "destination": destination,
            "overwritten": dst.exists() and overwrite,
        },
        "error": None,
    }


@tool
def move_file(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    """Move or rename a file inside the repository root.

    Both source and destination must be under the repo root. Use this to
    reorganize files or rename them.

    Args:
        source: Path relative to the repo root. Must exist.
        destination: Path relative to the repo root. Must not exist unless
            `overwrite` is true.
        overwrite: If true, replace an existing destination. Default false.

    Returns:
        `{ok, summary, data: {source, destination, overwritten}, error}`.
    """

    try:
        return move(source, destination, overwrite)
    except (MoveFileError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not move {source}",
            "data": {"source": source, "destination": destination},
            "error": {"code": "move_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error moving {source}",
            "data": {"source": source, "destination": destination},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = move_file
