"""Tests for tools/list-files/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "list_files_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _populate(root: Path) -> dict[str, Path]:
    """Create a predictable directory structure under `root`."""
    (root / "alpha.txt").write_text("a", encoding="utf-8")
    (root / "beta.md").write_text("bb", encoding="utf-8")
    (root / "subdir").mkdir()
    (root / "subdir" / "nested.py").write_text("x = 1\n", encoding="utf-8")
    (root / "zeta").mkdir()
    (root / ".hidden").write_text("secret", encoding="utf-8")
    (root / ".configdir").mkdir()
    return {
        "alpha": root / "alpha.txt",
        "beta": root / "beta.md",
        "subdir": root / "subdir",
        "zeta": root / "zeta",
        "hidden": root / ".hidden",
    }


def test_lists_dirs_before_files(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir(".")
    assert result["ok"] is True
    types = [entry["type"] for entry in result["data"]["entries"]]
    names = [entry["name"] for entry in result["data"]["entries"]]
    first_file_idx = next((i for i, t in enumerate(types) if t == "file"), len(types))
    assert all(t == "dir" for t in types[:first_file_idx])
    assert all(t == "file" for t in types[first_file_idx:])
    # Dotfiles are hidden by default.
    assert ".hidden" not in names
    assert ".configdir" not in names


def test_alphabetical_order_within_each_type(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir(".")
    names = [entry["name"] for entry in result["data"]["entries"]]
    # Directories come first, alphabetical: subdir, zeta (excluding .configdir)
    assert names[:2] == ["subdir", "zeta"]
    # Files alphabetically: alpha.txt, beta.md (excluding .hidden)
    assert names[2:] == ["alpha.txt", "beta.md"]


def test_include_hidden_returns_dotfiles(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir(".", include_hidden=True)
    names = [entry["name"] for entry in result["data"]["entries"]]
    assert ".hidden" in names
    assert ".configdir" in names


def test_file_entries_include_size(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir(".")
    files = {e["name"]: e for e in result["data"]["entries"] if e["type"] == "file"}
    assert files["alpha.txt"]["size"] == 1
    assert files["beta.md"]["size"] == 2


def test_dir_entries_have_no_size_key(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir(".")
    dirs = [e for e in result["data"]["entries"] if e["type"] == "dir"]
    assert all("size" not in d for d in dirs)


def test_list_nested_path(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir("subdir")
    names = [entry["name"] for entry in result["data"]["entries"]]
    assert names == ["nested.py"]


def test_default_path_lists_repo_root(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir()
    names = [entry["name"] for entry in result["data"]["entries"]]
    assert "alpha.txt" in names


def test_rejects_missing_directory(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.ListFilesError, match="directory not found"):
        mod.list_dir("does-not-exist")


def test_rejects_file_path(mod, tmp_path: Path) -> None:
    (tmp_path / "afile.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(mod.ListFilesError, match="not a directory"):
        mod.list_dir("afile.txt")


def test_rejects_traversal(mod, tmp_path: Path) -> None:
    with pytest.raises(Exception):  # PathEscapeError
        mod.list_dir("../../")


def test_truncates_at_max_entries(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "MAX_ENTRIES", 3)
    for i in range(10):
        (tmp_path / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    result = mod.list_dir(".")
    assert result["data"]["truncated"] is True
    assert len(result["data"]["entries"]) == 3


def test_summary_describes_count(mod, tmp_path: Path) -> None:
    _populate(tmp_path)
    result = mod.list_dir(".")
    # 2 dirs + 2 files = 4 visible entries.
    assert result["summary"] == "4 entries in ."
