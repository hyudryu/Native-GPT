"""Shared path-safety helpers for filesystem tools.

Lives under `tools/_lib/` so the Rust discovery (`crates/server/src/tools.rs`)
ignores it — discovery only looks at folders that contain BOTH `manifest.json`
and `tool.py`. Tools import via a relative path computation; see `resolve_lib`
below.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Resolve the repository root.

    Order: AGENTGPT_REPO_ROOT env var (what the runtime already uses), then
    cwd. Always returns an absolute, resolved path.
    """
    configured = os.environ.get("AGENTGPT_REPO_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path.cwd().resolve()


def allowed_roots() -> list[Path]:
    """Roots the filesystem tools may read from.

    Defaults to the repo root only. Extend with the colon-separated
    `AGENTGPT_ALLOWED_ROOTS` env var for later expansion (e.g. an indexed
    user-documents folder). All entries are resolved and deduplicated.
    """
    roots = [repo_root()]
    extra = os.environ.get("AGENTGPT_ALLOWED_ROOTS", "")
    for piece in extra.split(os.pathsep):
        piece = piece.strip()
        if not piece:
            continue
        roots.append(Path(piece).resolve())
    # Deduplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            real = root.resolve()
        except (OSError, RuntimeError):
            continue
        if real in seen:
            continue
        seen.add(real)
        unique.append(real)
    return unique


class PathEscapeError(ValueError):
    """Raised when a relative path would escape its containing root."""


def resolve_under_root(relative: str) -> Path:
    """Resolve `relative` against an allowed root, rejecting traversal escapes.

    `relative` may use forward or backslashes; it is interpreted relative to
    the repo root (or any allowed root). The result must satisfy:
      - the resolved path's parents include an allowed root, AND
      - no symlink in the resolved path points outside the root.

    Raises PathEscapeError if the path escapes every allowed root.
    """
    if not relative or not isinstance(relative, str):
        raise PathEscapeError("path must be a non-empty string")
    candidate = Path(relative)
    if candidate.is_absolute():
        # An absolute path is allowed only if it already lives under a root.
        resolved = candidate.resolve(strict=False)
    else:
        resolved = (repo_root() / candidate).resolve(strict=False)

    roots = allowed_roots()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PathEscapeError(
        f"path {relative!r} resolves outside all allowed roots"
    )
