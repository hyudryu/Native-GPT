"""Tests for tools/_lib/paths.py.

Run from the repo root with:
    uv run pytest tools/_lib/test_paths.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make _lib importable when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402  (sys.path bootstrap above)


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Treat `tmp_path` as the repo root and clear the allowlist."""
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    return tmp_path


def test_resolve_under_root_accepts_simple_relative(isolated_root: Path) -> None:
    (isolated_root / "hello.txt").write_text("hi", encoding="utf-8")
    resolved = paths.resolve_under_root("hello.txt")
    assert resolved == (isolated_root / "hello.txt").resolve()


def test_resolve_under_root_accepts_nested_relative(isolated_root: Path) -> None:
    nested = isolated_root / "sub" / "dir"
    nested.mkdir(parents=True)
    target = nested / "f.txt"
    target.write_text("x", encoding="utf-8")
    resolved = paths.resolve_under_root("sub/dir/f.txt")
    assert resolved == target.resolve()


def test_resolve_under_root_rejects_traversal(isolated_root: Path) -> None:
    (isolated_root / "inside.txt").write_text("ok", encoding="utf-8")
    with pytest.raises(paths.PathEscapeError):
        paths.resolve_under_root("../../etc/passwd")


def test_resolve_under_root_rejects_absolute_outside_root(
    isolated_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("nope", encoding="utf-8")
    with pytest.raises(paths.PathEscapeError):
        paths.resolve_under_root(str(secret))


def test_resolve_under_root_rejects_empty(isolated_root: Path) -> None:
    with pytest.raises(paths.PathEscapeError):
        paths.resolve_under_root("")


def test_allowed_roots_respects_env(
    isolated_root: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path_factory.mktemp("extra")
    monkeypatch.setenv("AGENTGPT_ALLOWED_ROOTS", str(extra))
    roots = paths.allowed_roots()
    assert isolated_root in roots
    assert extra.resolve() in roots


def test_resolve_under_root_works_under_extra_root(
    isolated_root: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path_factory.mktemp("extra")
    target = extra / "doc.md"
    target.write_text("# doc", encoding="utf-8")
    monkeypatch.setenv("AGENTGPT_ALLOWED_ROOTS", str(extra))
    resolved = paths.resolve_under_root(str(target))
    assert resolved == target.resolve()


def test_allowed_roots_ignores_blank_entries(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTGPT_ALLOWED_ROOTS", os.pathsep + os.pathsep)
    roots = paths.allowed_roots()
    # Only the repo root remains; blanks didn't add anything.
    assert roots == [isolated_root]
