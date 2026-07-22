"""Search files Strands tool.

Keyword search across text files under the repository root. Prefers ripgrep
(`rg`) when on PATH for speed and .gitignore awareness, then falls back to a
pure-Python walk. Both paths honor the same `tools/_lib/paths.py` allowlist
and cap results at `limit` matches.

The pure-Python fallback is the canonical testable path. The rg path is
thin: it shells out and parses lines.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from strands import tool

# Load the shared `_lib/paths.py`.
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
allowed_roots = _paths.allowed_roots

MAX_MATCHES = 200  # hard ceiling regardless of `limit`
DEFAULT_LIMIT = 50
MAX_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MB
BINARY_DETECT_BYTES = 2048

# File extensions we'll even attempt to read. Picking a broad allowlist keeps
# us out of huge binaries and lockfiles by default. Empty list means "all".
DEFAULT_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".md", ".txt", ".rst", ".sql", ".html", ".css", ".scss",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cfg", ".ini", ".env",
    ".java", ".kt", ".go", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".scala", ".clj", ".ex", ".exs", ".erl",
    ".vim", ".lua", ".r", ".dart",
})


class SearchFilesError(ValueError):
    """Raised for any search_files failure (bad query, forbidden path, etc.)."""


def _normalize_file_types(file_types: Sequence[str] | None) -> set[str] | None:
    if file_types is None:
        return None
    normalized: set[str] = set()
    for ft in file_types:
        if not isinstance(ft, str):
            continue
        # Accept "py", ".py", "*.py", " *.py " — normalize to ".py".
        cleaned = ft.strip().lstrip("*.").strip(".").lower()
        if cleaned:
            normalized.add(f".{cleaned}")
    return normalized or None


def _looks_binary(prefix: bytes) -> bool:
    return b"\x00" in prefix


def _walk_and_search(
    roots: list[Path],
    pattern: re.Pattern[str],
    file_types: set[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Pure-Python fallback: walk `roots`, grep `pattern` line by line."""
    matches: list[dict[str, Any]] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip common VCS / dependency dirs. rg respects .gitignore; we
            # approximate by hard-skipping the usual offenders.
            dirnames[:] = [
                d for d in dirnames
                if d not in {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"}
                and not d.startswith(".")
            ]
            for filename in filenames:
                if len(matches) >= limit:
                    return matches
                ext = Path(filename).suffix.lower()
                if file_types is not None and ext not in file_types:
                    continue
                if ext == "" and file_types is not None:
                    continue  # extensionless file when filtering by ext
                full = Path(dirpath) / filename
                try:
                    if full.is_symlink():
                        continue
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                    with full.open("rb") as fp:
                        prefix = fp.read(BINARY_DETECT_BYTES)
                    if _looks_binary(prefix):
                        continue
                    text = full.read_text(encoding="utf-8", errors="replace")
                except (OSError, PermissionError):
                    continue
                for line_no, line in enumerate(text.splitlines(), start=1):
                    m = pattern.search(line)
                    if m is None:
                        continue
                    matches.append({
                        "path": str(full.relative_to(root)) if full.is_relative_to(root) else str(full),
                        "line_number": line_no,
                        "line": line,
                        "match_start": m.start(),
                        "match_end": m.end(),
                    })
                    if len(matches) >= limit:
                        return matches
    return matches


def _run_ripgrep(
    roots: list[Path],
    pattern: re.Pattern[str],
    file_types: set[str] | None,
    limit: int,
    *,
    is_regex: bool = False,
    runner: Any = None,
) -> list[dict[str, Any]] | None:
    """Try to run ripgrep. Returns parsed matches, or None if rg is unavailable.

    `runner` is injectable for tests; default uses subprocess.run.
    """
    rg = shutil.which("rg")
    if rg is None:
        return None

    cmd: list[str] = [
        rg,
        "--no-heading",
        "--line-number",
        "--color", "never",
        "--no-binary",
        "--max-count", str(limit),
        "--json",
    ]
    if file_types is not None:
        for ext in sorted(file_types):
            cmd.extend(["--glob", f"*{ext}"])
    if not is_regex:
        cmd.append("--fixed-strings")
    cmd.append("--")
    cmd.append(pattern.pattern)
    cmd.extend(str(root) for root in roots)

    run = runner or (lambda argv: subprocess.run(  # noqa: S603 — argv is constructed, not user input
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    ))
    try:
        completed = run(cmd)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode not in (0, 1):  # 1 = "no matches", which is fine
        return None

    return _parse_rg_json(completed.stdout, limit)


def _parse_rg_json(stdout: str, limit: int) -> list[dict[str, Any]]:
    """Parse rg's --json output into our match dict shape.

    rg emits a stream of JSON objects, one per line. We care about
    `{"type":"match",...,"data":{path,text,line_number,submatches:[...]}}`.
    """
    import json  # noqa: PLC0415 — local import keeps tool import cheap

    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if len(matches) >= limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "match":
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        path_obj = data.get("path")
        if isinstance(path_obj, dict):
            path = path_obj.get("text", "")
        else:
            path = str(path_obj) if path_obj is not None else ""
        text_obj = data.get("lines", {}).get("text", "")
        line_text = text_obj if isinstance(text_obj, str) else str(text_obj)
        line_text = line_text.rstrip("\n")
        submatches = data.get("submatches", [])
        first = submatches[0] if submatches else {}
        start = first.get("start", 0) if isinstance(first, dict) else 0
        end = first.get("end", len(line_text)) if isinstance(first, dict) else len(line_text)
        matches.append({
            "path": path,
            "line_number": data.get("line_number", 0),
            "line": line_text,
            "match_start": start,
            "match_end": end,
        })
    return matches


def search(
    query: str,
    file_types: Sequence[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    *,
    regex: bool = False,
    runner: Any = None,
    skip_ripgrep: bool = False,
) -> dict[str, Any]:
    """Run a keyword search under the repo root. Standard-schema result dict.

    By default `query` is a literal substring; pass `regex=True` to interpret
    it as a Python regular expression. `runner` injects a subprocess runner
    (for tests); `skip_ripgrep` forces the pure-Python fallback.
    """
    if not isinstance(query, str) or not query.strip():
        raise SearchFilesError("query must be a non-empty string")
    try:
        limit_int = int(limit)
    except (TypeError, ValueError) as exc:
        raise SearchFilesError(f"limit must be an integer (got {limit!r})") from exc
    if limit_int < 1:
        raise SearchFilesError("limit must be >= 1")
    limit_int = min(limit_int, MAX_MATCHES)

    types_set = _normalize_file_types(file_types)
    # Literal substring search by default — re.escape so "2 + 2" matches
    # literally. With regex=True we compile the raw pattern.
    try:
        pattern = re.compile(query if regex else re.escape(query))
    except re.error as exc:
        raise SearchFilesError(f"invalid regex: {exc}") from exc

    roots = allowed_roots()
    if not roots:
        raise SearchFilesError("no allowed roots are configured")

    matches: list[dict[str, Any]] | None = None
    if not skip_ripgrep:
        matches = _run_ripgrep(roots, pattern, types_set, limit_int, runner=runner, is_regex=regex)
    if matches is None:
        matches = _walk_and_search(roots, pattern, types_set, limit_int)

    summary = f"{len(matches)} match{'es' if len(matches) != 1 else ''} for {query!r}"
    return {
        "ok": True,
        "summary": summary,
        "data": {
            "query": query,
            "file_types": sorted(types_set) if types_set else None,
            "matches": matches,
            "limit": limit_int,
            "truncated": len(matches) >= limit_int,
        },
        "error": None,
    }


@tool
def search_files(
    query: str,
    file_types: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Search file contents under the repository root for `query`.

    Tries ripgrep first (fast, respects .gitignore); falls back to a pure
    Python walk if rg is unavailable. Use this to find where something is
    defined or referenced before opening files with read_file.

    `query` is matched as a literal substring (so `"2 + 2"` and `"calculate"`
    both find exactly those strings). Regex metacharacters in the query are
    treated as plain text; if you need regex, call the underlying `search`
    helper with `regex=True`.

    Args:
        query: A substring to search for.
        file_types: Optional list of extensions to restrict the search, e.g.
            `["py", "ts"]`. Accepts "py", ".py", or "*.py".
        limit: Maximum number of matches to return (1-200, default 50).

    Returns:
        `{ok, summary, data: {query, file_types?, matches: [{path, line_number,
        line, match_start, match_end}], limit, truncated}, error}`.
    """

    try:
        return search(query, file_types, limit)
    except SearchFilesError as exc:
        return {
            "ok": False,
            "summary": "Search rejected",
            "data": {"query": query},
            "error": {"code": "search_error", "message": str(exc)},
        }
    except (OSError, re.error) as exc:
        return {
            "ok": False,
            "summary": "Search failed",
            "data": {"query": query},
            "error": {"code": "search_runtime_error", "message": str(exc)},
        }


TOOL = search_files
