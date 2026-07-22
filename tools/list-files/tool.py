"""List files Strands tool.

Lists immediate children of a directory under the repository root. Uses
`tools/_lib/paths.py` for the path-safety policy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from strands import tool

# Load the shared `_lib/paths.py`. See tools/read-file/tool.py for the import
# pattern rationale.
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
resolve_under_root = _paths.resolve_under_root
PathEscapeError = _paths.PathEscapeError

MAX_ENTRIES = 500


class ListFilesError(ValueError):
    """Raised for any list_files failure (missing dir, forbidden path, etc.)."""


def list_dir(path: str = ".", include_hidden: bool = False) -> dict[str, Any]:
    """List immediate children of `path` and return a standard-schema result dict."""
    if not isinstance(path, str):
        raise ListFilesError("path must be a string")
    resolved = resolve_under_root(path)
    if not resolved.exists():
        raise ListFilesError(f"directory not found: {path}")
    if not resolved.is_dir():
        raise ListFilesError(f"not a directory: {path}")

    try:
        children = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError) as exc:
        raise ListFilesError(f"cannot read directory {path}: {exc}") from exc

    entries: list[dict[str, Any]] = []
    truncated = False
    for child in children:
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        name = child.name
        if not include_hidden and name.startswith("."):
            continue
        if child.is_dir():
            entries.append({"name": name, "type": "dir"})
        elif child.is_file():
            try:
                size = child.stat().st_size
            except OSError:
                size = None
            entries.append({"name": name, "type": "file", "size": size})
        else:
            # Symlinks, sockets, FIFOs, etc. — report as "other".
            entries.append({"name": name, "type": "other"})

    summary = f"{len(entries)} entr{'ies' if len(entries) != 1 else 'y'} in {path}"
    return {
        "ok": True,
        "summary": summary,
        "data": {
            "path": path,
            "entries": entries,
            "truncated": truncated,
        },
        "error": None,
    }


@tool
def list_files(path: str = ".", include_hidden: bool = False) -> dict[str, Any]:
    """List the immediate contents of a directory under the repository root.

    Use this to discover what files exist before reading them. Returns names,
    types (file/dir/other), and file sizes. Directories are listed before
    files, alphabetically. Dotfiles are hidden unless `include_hidden=true`.

    Args:
        path: Directory relative to the repo root (default: the root itself).
        include_hidden: If true, include entries whose name starts with a dot.

    Returns:
        `{ok, summary, data: {path, entries: [{name, type, size?}], truncated}, error}`.
    """

    try:
        return list_dir(path, include_hidden)
    except (ListFilesError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not list {path}",
            "data": {"path": path},
            "error": {"code": "list_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error listing {path}",
            "data": {"path": path},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = list_files
