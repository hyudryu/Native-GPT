"""Tests for tools/delete-file/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "delete_file_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def test_deletes_existing_file(mod, tmp_path: Path) -> None:
    (tmp_path / "garbage.txt").write_text("x" * 100, encoding="utf-8")
    result = mod.delete("garbage.txt")
    assert result["ok"] is True
    assert not (tmp_path / "garbage.txt").exists()
    assert result["data"]["bytes_freed"] == 100


def test_rejects_missing_file(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.DeleteFileError, match="file not found"):
        mod.delete("never-existed.txt")


def test_refuses_directory(mod, tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "inner.txt").write_text("x", encoding="utf-8")
    with pytest.raises(mod.DeleteFileError, match="not a regular file"):
        mod.delete("subdir")
    # Directory and contents are still there.
    assert (tmp_path / "subdir" / "inner.txt").exists()


def test_rejects_traversal(mod, tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Exception):
        mod.delete("../../etc/passwd")


def test_rejects_empty_path(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.DeleteFileError):
        mod.delete("")


def test_summary_includes_size(mod, tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("hello", encoding="utf-8")
    result = mod.delete("x.txt")
    assert "5 bytes" in result["summary"]
