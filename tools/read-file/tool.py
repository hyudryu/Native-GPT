"""Read file Strands tool.

Reads text from a file under the repository root (or any `AGENTGPT_ALLOWED_ROOTS`
entry). Path safety, traversal rejection, and the allowed-root policy live in
`tools/_lib/paths.py`, which is loaded by file path (the runtime's tool loader
imports each tool.py as a standalone module, so normal package imports don't
work across tool folders).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from strands import tool

# Load the shared `_lib/paths.py` as a module. The runtime imports each tool's
# `tool.py` via spec_from_file_location, so this file's package context is not
# available. We compute the lib path from __file__ and import it directly.
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
resolve_under_root = _paths.resolve_under_root
PathEscapeError = _paths.PathEscapeError

MAX_BYTES = 256 * 1024  # 256 KB cap on the read size
BINARY_DETECT_BYTES = 2048  # first 2 KB checked for NUL to detect binary


class ReadFileError(ValueError):
    """Raised for any read_file failure (missing, binary, oversized, forbidden)."""


def _looks_binary(prefix: bytes) -> bool:
    return b"\x00" in prefix


def read(path: str, offset: int = 0, length: int = 2000) -> dict[str, Any]:
    """Read `path` and return a standard-schema result dict.

    `offset` and `length` are line counts (1-indexed offset, positive count).
    """
    if not isinstance(path, str) or not path.strip():
        raise ReadFileError("path must be a non-empty string")
    try:
        offset_int = int(offset)
        length_int = int(length)
    except (TypeError, ValueError) as exc:
        raise ReadFileError(f"offset/length must be integers") from exc
    if offset_int < 0:
        raise ReadFileError("offset must be >= 0")
    if length_int < 1:
        raise ReadFileError("length must be >= 1")

    resolved = resolve_under_root(path)
    if not resolved.exists():
        raise ReadFileError(f"file not found: {path}")
    if not resolved.is_file():
        raise ReadFileError(f"not a regular file: {path}")

    # Binary guard: peek at the first chunk before reading the whole file.
    with resolved.open("rb") as binary:
        prefix = binary.read(BINARY_DETECT_BYTES)
    if _looks_binary(prefix):
        raise ReadFileError(f"file appears to be binary (NUL bytes detected): {path}")

    # Decode the full file (we already know it's not binary).
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = resolved.read_text(encoding="latin-1", errors="replace")

    all_lines = text.splitlines()
    total_lines = len(all_lines)
    if total_lines == 0:
        return {
            "ok": True,
            "summary": f"empty file: {path}",
            "data": {
                "path": path,
                "total_lines": 0,
                "offset": offset_int,
                "length": 0,
                "lines": [],
                "truncated": False,
            },
            "error": None,
        }

    start = min(offset_int, total_lines)
    end = min(start + length_int, total_lines)
    sliced = all_lines[start:end]
    truncated = end < total_lines or start > 0  # any windowing counts as truncated

    # Cap total payload size.
    joined = "\n".join(sliced)
    if len(joined.encode("utf-8")) > MAX_BYTES:
        # Hard cap: keep slicing until we fit. Drop trailing lines.
        keep: list[str] = []
        running = 0
        for line in sliced:
            size = len(line.encode("utf-8")) + 1
            if running + size > MAX_BYTES:
                break
            keep.append(line)
            running += size
        sliced = keep
        truncated = True

    summary = f"{len(sliced)} line{'s' if len(sliced) != 1 else ''} of {path} (from line {start + 1})"
    return {
        "ok": True,
        "summary": summary,
        "data": {
            "path": path,
            "total_lines": total_lines,
            "offset": start,
            "length": len(sliced),
            "lines": sliced,
            "truncated": truncated,
        },
        "error": None,
    }


@tool
def read_file(path: str, offset: int = 0, length: int = 2000) -> dict[str, Any]:
    """Read text from a file under the repository root, with paging.

    Use this to inspect source code, configuration, or notes the agent should
    reason about. Lines are 1-indexed in the UI; `offset` is the 0-indexed
    starting line, `length` is the number of lines to return.

    Args:
        path: A path relative to the repo root, or an absolute path inside an
            allowed root. Traversal (`..`) outside the root is rejected.
        offset: 0-indexed line to start reading from (default 0).
        length: Number of lines to return (default 2000).

    Returns:
        `{ok, summary, data: {path, total_lines, offset, length, lines, truncated}, error}`.
    """

    try:
        return read(path, offset, length)
    except (ReadFileError, PathEscapeError) as exc:
        return {
            "ok": False,
            "summary": f"Could not read {path}",
            "data": {"path": path},
            "error": {"code": "read_error", "message": str(exc)},
        }
    except OSError as exc:
        return {
            "ok": False,
            "summary": f"OS error reading {path}",
            "data": {"path": path},
            "error": {"code": "io_error", "message": str(exc)},
        }


TOOL = read_file
