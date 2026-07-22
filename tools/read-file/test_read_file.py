"""Tests for tools/read-file/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "read_file_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set the repo root to tmp_path for tests that need it directly."""
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    return tmp_path


def test_reads_existing_file(mod, isolated_root: Path) -> None:
    target = isolated_root / "hello.txt"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    result = mod.read("hello.txt")
    assert result["ok"] is True
    assert result["data"]["total_lines"] == 3
    assert result["data"]["lines"] == ["line one", "line two", "line three"]
    assert result["data"]["truncated"] is False


def test_offset_and_length(mod, isolated_root: Path) -> None:
    target = isolated_root / "f.txt"
    target.write_text("\n".join(f"line {i}" for i in range(20)) + "\n", encoding="utf-8")
    result = mod.read("f.txt", offset=5, length=3)
    assert result["data"]["offset"] == 5
    assert result["data"]["length"] == 3
    assert result["data"]["lines"] == ["line 5", "line 6", "line 7"]
    assert result["data"]["truncated"] is True


def test_offset_past_end_returns_empty_window(mod, isolated_root: Path) -> None:
    target = isolated_root / "small.txt"
    target.write_text("only\n", encoding="utf-8")
    result = mod.read("small.txt", offset=10)
    assert result["data"]["length"] == 0
    assert result["data"]["lines"] == []


def test_empty_file_returns_zero_lines(mod, isolated_root: Path) -> None:
    (isolated_root / "empty.txt").write_text("", encoding="utf-8")
    result = mod.read("empty.txt")
    assert result["ok"] is True
    assert result["data"]["total_lines"] == 0
    assert result["data"]["lines"] == []


def test_rejects_missing_file(mod, isolated_root: Path) -> None:
    with pytest.raises(mod.ReadFileError, match="file not found"):
        mod.read("does-not-exist.txt")


def test_rejects_directory(mod, isolated_root: Path) -> None:
    (isolated_root / "subdir").mkdir()
    with pytest.raises(mod.ReadFileError, match="not a regular file"):
        mod.read("subdir")


def test_rejects_traversal(mod, isolated_root: Path) -> None:
    (isolated_root / "inside.txt").write_text("ok", encoding="utf-8")
    with pytest.raises(Exception):  # PathEscapeError leaks through
        mod.read("../../etc/passwd")


def test_rejects_binary_file(mod, isolated_root: Path) -> None:
    binary = isolated_root / "data.bin"
    binary.write_bytes(b"some\x00binary\x00data")
    with pytest.raises(mod.ReadFileError, match="binary"):
        mod.read("data.bin")


def test_rejects_empty_path(mod, isolated_root: Path) -> None:
    with pytest.raises(mod.ReadFileError):
        mod.read("")


def test_rejects_negative_offset(mod, isolated_root: Path) -> None:
    (isolated_root / "x.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(mod.ReadFileError):
        mod.read("x.txt", offset=-1)


def test_rejects_zero_length(mod, isolated_root: Path) -> None:
    (isolated_root / "x.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(mod.ReadFileError):
        mod.read("x.txt", length=0)


def test_utf8_and_latin1_fallback(mod, isolated_root: Path) -> None:
    target = isolated_root / "weird.txt"
    target.write_bytes("café résumé\n".encode("utf-8"))
    result = mod.read("weird.txt")
    assert "café" in result["data"]["lines"][0]


def test_path_is_relative_to_repo_root(mod, isolated_root: Path) -> None:
    nested = isolated_root / "sub" / "dir"
    nested.mkdir(parents=True)
    target = nested / "f.txt"
    target.write_text("hi\n", encoding="utf-8")
    result = mod.read("sub/dir/f.txt")
    assert result["data"]["lines"] == ["hi"]
