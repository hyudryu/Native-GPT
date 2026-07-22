"""Write file Strands tool.

Atomically writes text content to a file under the repository root. Path
safety (traversal rejection, root confinement) is provided by
`tools/_lib/paths.py`. Caps content size to bound disk usage.
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

MAX_BYTES = 1024 * 1024  # 1 MB cap on write size


class WriteFileError(ValueError):
    """Raised for any write_file failure (size cap, traversal, IO)."""


def write(path: str, content: str, create_dirs: bool = True) -> dict[str, Any]:
    """Write `content` to `path` atomically. Standard-schema result dict."""
    if not isinstance(path, str) or not path.strip():
        raise WriteFileError("path must be a non-empty string")
    if not isinstance(content, str):
        raise WriteFileError("content must be a string")
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise WriteFileError(
            f"content exceeds {MAX_BYTES} byte cap (got {len(content.encode('utf-8'))})"
        )

    resolved = resolve_under_root(path)
    parent = resolved.parent

    # Parent must be under an allowed root too — resolve_under_root already
    # verified `resolved`, but a parent check guards against the (theoretical)
    # case of a file at the root boundary.
    if create_dirs:
        parent.mkdir(parents=True, exist_ok=True)
    elif not parent.is_dir():
        raise WriteFileError(f"parent directory does not exist: {parent} (pass create_dirs=True)")

    # Atomic write: tmp file in the same directory, then rename.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".agentgpt-write-", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            fp.write(content)
        os.replace(tmp_path, resolved)
    except Exception:
        # Clean up the tmp file on any failure; don't leak it.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "summary": f"wrote {len(content)} chars to {path}",
        "data": {
            "path": path,
            "bytes": len(content.encode("utf-8")),
            "created_dirs": create_dirs and not parent.exists(),
        },
        "error": None,
    }


@tool
def write_file(path: str, content: str, create_dirs: bool = True) -> dict[str, Any]:
    """Write text content to a file under the repository root, replacing any existing content.

    Use this to create or overwrite source files, notes, or config. Atomic:
    the file is either fully written or unchanged. Parent directories are
    created by default.

    Args:
        path: A path relative to the repo root. Traversal (`..`) outside the
            root is rejected.
        content: The text to write (UTF-8). Capped at 1 MB.
        create_dirs: If true (default), create parent directories as needed.

    Returns:
        `{ok, summary, data: {path, bytes, created_dirs}, error}`.
    """

    try:
        return write(path, content, create_dirs)
    except (WriteFileError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not write {path}",
            "data": {"path": path},
            "error": {"code": "write_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error writing {path}",
            "data": {"path": path},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = write_file
