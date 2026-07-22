"""Tests for tools/move-file/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "move_file_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def test_renames_file(mod, tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("content", encoding="utf-8")
    result = mod.move("old.txt", "new.txt")
    assert result["ok"] is True
    assert not (tmp_path / "old.txt").exists()
    assert (tmp_path / "new.txt").read_text() == "content"


def test_moves_into_subdirectory(mod, tmp_path: Path) -> None:
    (tmp_path / "root.txt").write_text("x", encoding="utf-8")
    mod.move("root.txt", "sub/dir/moved.txt")
    assert (tmp_path / "sub" / "dir" / "moved.txt").read_text() == "x"
    assert not (tmp_path / "root.txt").exists()


def test_rejects_missing_source(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.MoveFileError, match="source not found"):
        mod.move("nope.txt", "elsewhere.txt")


def test_rejects_source_directory(mod, tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    with pytest.raises(mod.MoveFileError, match="not a regular file"):
        mod.move("dir", "elsewhere")


def test_rejects_existing_destination_by_default(mod, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    with pytest.raises(mod.MoveFileError, match="already exists"):
        mod.move("a.txt", "b.txt")


def test_overwrite_true_replaces_destination(mod, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a-content", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b-content", encoding="utf-8")
    result = mod.move("a.txt", "b.txt", overwrite=True)
    assert result["ok"] is True
    assert (tmp_path / "b.txt").read_text() == "a-content"
    assert not (tmp_path / "a.txt").exists()


def test_rejects_traversal_source(mod, tmp_path: Path) -> None:
    with pytest.raises(Exception):
        mod.move("../../etc/passwd", "inside.txt")


def test_rejects_traversal_destination(mod, tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Exception):
        mod.move("x.txt", "../../escape.txt")


def test_rejects_empty_args(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.MoveFileError):
        mod.move("", "x")
    with pytest.raises(mod.MoveFileError):
        mod.move("x", "")
