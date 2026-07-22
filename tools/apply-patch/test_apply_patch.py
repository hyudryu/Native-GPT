"""Tests for tools/apply-patch/tool.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "apply_patch_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _seed(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f


def test_single_edit_replaces_unique_match(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.py", "def foo():\n    return 1\n")
    result = mod.apply("f.py", {"find": "return 1", "replace": "return 2"})
    assert result["ok"] is True
    assert (tmp_path / "f.py").read_text() == "def foo():\n    return 2\n"
    assert result["data"]["replacements"] == 1


def test_list_of_edits_applied_sequentially(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.py", "a = 1\nb = 2\nc = 3\n")
    result = mod.apply("f.py", [
        {"find": "a = 1", "replace": "a = 10"},
        {"find": "c = 3", "replace": "c = 30"},
    ])
    assert result["ok"] is True
    assert (tmp_path / "f.py").read_text() == "a = 10\nb = 2\nc = 30\n"
    assert result["data"]["edits_applied"] == 2


def test_all_true_replaces_every_occurrence(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.txt", "foo foo foo")
    result = mod.apply("f.txt", {"find": "foo", "replace": "bar", "all": True})
    assert (tmp_path / "f.txt").read_text() == "bar bar bar"
    assert result["data"]["replacements"] == 3


def test_default_rejects_multiple_matches(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.txt", "x x x")
    with pytest.raises(mod.ApplyPatchError, match="3 times"):
        mod.apply("f.txt", {"find": "x", "replace": "y"})


def test_rejects_missing_find_string(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.txt", "hello")
    with pytest.raises(mod.ApplyPatchError, match="not present"):
        mod.apply("f.txt", {"find": "missing", "replace": "x"})


def test_rejects_empty_find(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.txt", "hello")
    with pytest.raises(mod.ApplyPatchError, match="empty"):
        mod.apply("f.txt", {"find": "", "replace": "x"})


def test_atomic_failure_leaves_file_unchanged(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.py", "def foo():\n    return 1\n")
    original = (tmp_path / "f.py").read_text()
    with pytest.raises(mod.ApplyPatchError):
        # Second edit will fail — file must be unchanged.
        mod.apply("f.py", [
            {"find": "return 1", "replace": "return 2"},
            {"find": "nonexistent", "replace": "x"},
        ])
    assert (tmp_path / "f.py").read_text() == original


def test_rejects_missing_file(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.ApplyPatchError, match="file not found"):
        mod.apply("nope.txt", {"find": "x", "replace": "y"})


def test_rejects_directory_path(mod, tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    with pytest.raises(mod.ApplyPatchError, match="not a regular file"):
        mod.apply("subdir", {"find": "x", "replace": "y"})


def test_rejects_traversal(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "inside.txt", "x")
    with pytest.raises(Exception):
        mod.apply("../../etc/passwd", {"find": "x", "replace": "y"})


def test_rejects_too_many_edits(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "MAX_EDITS", 3)
    _seed(tmp_path, "f.txt", "a\nb\nc\nd\n")
    with pytest.raises(mod.ApplyPatchError, match="too many"):
        mod.apply("f.txt", [
            {"find": "a", "replace": "1"},
            {"find": "b", "replace": "2"},
            {"find": "c", "replace": "3"},
            {"find": "d", "replace": "4"},
        ])


def test_normalize_rejects_non_dict_edits(mod) -> None:
    with pytest.raises(mod.ApplyPatchError):
        mod._normalize_edits("not a list")  # type: ignore[arg-type]
    with pytest.raises(mod.ApplyPatchError):
        mod._normalize_edits([1, 2])  # type: ignore[list-item]


def test_no_op_returns_zero_replacements(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.txt", "abc")
    # find and replace are identical — net content is unchanged.
    result = mod.apply("f.txt", {"find": "abc", "replace": "abc"})
    assert result["ok"] is True
    assert result["data"]["replacements"] == 0
    assert result["summary"] == "no changes to f.txt"


def test_summary_describes_count(mod, tmp_path: Path) -> None:
    _seed(tmp_path, "f.txt", "x")
    result = mod.apply("f.txt", {"find": "x", "replace": "y"})
    assert "1 edit" in result["summary"]
    assert "1 replacement" in result["summary"]
