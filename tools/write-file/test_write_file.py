"""Tests for tools/write-file/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "write_file_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def test_writes_new_file(mod, tmp_path: Path) -> None:
    result = mod.write("hello.txt", "hello world")
    assert result["ok"] is True
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello world"
    assert result["data"]["bytes"] == 11


def test_overwrites_existing_file(mod, tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("old", encoding="utf-8")
    mod.write("f.txt", "new contents")
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new contents"


def test_creates_parent_directories_by_default(mod, tmp_path: Path) -> None:
    mod.write("sub/dir/deep.txt", "x")
    assert (tmp_path / "sub" / "dir" / "deep.txt").read_text() == "x"


def test_create_dirs_false_rejects_missing_parent(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.WriteFileError, match="parent directory does not exist"):
        mod.write("missing/f.txt", "x", create_dirs=False)


def test_rejects_traversal(mod, tmp_path: Path) -> None:
    (tmp_path / "inside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Exception):
        mod.write("../../escape.txt", "x")


def test_rejects_empty_path(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.WriteFileError):
        mod.write("", "x")


def test_rejects_non_string_content(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.WriteFileError):
        mod.write("x.txt", 123)  # type: ignore[arg-type]


def test_rejects_oversized_content(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "MAX_BYTES", 16)
    with pytest.raises(mod.WriteFileError, match="exceeds"):
        mod.write("big.txt", "x" * 100)


def test_atomic_no_tmp_file_left_on_success(mod, tmp_path: Path) -> None:
    mod.write("clean.txt", "ok")
    # No stray tmp files in the root.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".agentgpt-write-")]
    assert leftovers == []


def test_summary_includes_char_count(mod, tmp_path: Path) -> None:
    result = mod.write("x.txt", "abc")
    assert "3 chars" in result["summary"]


def test_unicode_content_round_trips(mod, tmp_path: Path) -> None:
    mod.write("u.txt", "café — résumé")
    assert (tmp_path / "u.txt").read_text(encoding="utf-8") == "café — résumé"
