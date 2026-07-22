"""Tests for tools/create-directory/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "create_directory_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def test_creates_single_directory(mod, tmp_path: Path) -> None:
    result = mod.create("newdir")
    assert result["ok"] is True
    assert result["data"]["already_existed"] is False
    assert (tmp_path / "newdir").is_dir()


def test_creates_nested_parents(mod, tmp_path: Path) -> None:
    mod.create("a/b/c/d")
    assert (tmp_path / "a" / "b" / "c" / "d").is_dir()


def test_idempotent_on_existing_directory(mod, tmp_path: Path) -> None:
    (tmp_path / "exists").mkdir()
    result = mod.create("exists")
    assert result["ok"] is True
    assert result["data"]["already_existed"] is True
    assert (tmp_path / "exists").is_dir()


def test_refuses_when_path_is_a_file(mod, tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    with pytest.raises(mod.CreateDirectoryError, match="not a directory"):
        mod.create("afile")


def test_rejects_traversal(mod, tmp_path: Path) -> None:
    with pytest.raises(Exception):
        mod.create("../../escape")


def test_rejects_empty_path(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.CreateDirectoryError):
        mod.create("")


def test_summary_reflects_already_existed(mod, tmp_path: Path) -> None:
    (tmp_path / "e").mkdir()
    result = mod.create("e")
    assert "already existed" in result["summary"]
