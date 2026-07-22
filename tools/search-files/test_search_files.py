"""Tests for tools/search-files/tool.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "search_files_tool_under_test"


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTGPT_ALLOWED_ROOTS", raising=False)
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _seed_corpus(root: Path) -> None:
    (root / "a.py").write_text("def calculate():\n    return 17 * 23\n", encoding="utf-8")
    (root / "b.py").write_text("# TODO: rename calculate to evaluate\n", encoding="utf-8")
    (root / "c.md").write_text("# Title\n\nMentions `calculate` here.\n", encoding="utf-8")
    (root / "data.bin").write_bytes(b"calculate\x00\x01\x02binary")  # binary, should be skipped
    sub = root / "pkg"
    sub.mkdir()
    (sub / "core.py").write_text("VALUE = 17 * 23\n", encoding="utf-8")


def test_finds_substring_across_files(mod, tmp_path: Path) -> None:
    _seed_corpus(tmp_path)
    # Force Python fallback so the test is deterministic and doesn't depend on rg.
    result = mod.search("calculate", skip_ripgrep=True)
    assert result["ok"] is True
    paths = {m["path"].replace(os.sep, "/") for m in result["data"]["matches"]}
    assert "a.py" in paths
    assert "b.py" in paths
    assert "c.md" in paths
    assert all("data.bin" not in p for p in paths)


def test_matches_include_line_number_and_offsets(mod, tmp_path: Path) -> None:
    _seed_corpus(tmp_path)
    result = mod.search("17 * 23", skip_ripgrep=True)
    a_matches = [m for m in result["data"]["matches"] if m["path"].replace(os.sep, "/") == "a.py"]
    assert len(a_matches) == 1
    m = a_matches[0]
    assert m["line_number"] == 2  # 1-indexed
    assert m["line"].strip() == "return 17 * 23"
    assert m["line"][m["match_start"]:m["match_end"]] == "17 * 23"


def test_limit_caps_results(mod, tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"f{i:02d}.py").write_text("# keyword match\n", encoding="utf-8")
    result = mod.search("keyword", limit=5, skip_ripgrep=True)
    assert len(result["data"]["matches"]) == 5
    assert result["data"]["truncated"] is True


def test_file_types_filter_restricts_extensions(mod, tmp_path: Path) -> None:
    _seed_corpus(tmp_path)
    result = mod.search("calculate", file_types=["py"], skip_ripgrep=True)
    paths = {m["path"].replace(os.sep, "/") for m in result["data"]["matches"]}
    assert all(p.endswith(".py") for p in paths)
    assert "c.md" not in paths


def test_file_types_accepts_various_input_forms(mod) -> None:
    assert mod._normalize_file_types(["py", ".ts", "*.js"]) == {".py", ".ts", ".js"}
    assert mod._normalize_file_types(None) is None
    assert mod._normalize_file_types([]) is None
    assert mod._normalize_file_types(["", "  ", "py"]) == {".py"}


def test_recursive_search_finds_nested_files(mod, tmp_path: Path) -> None:
    _seed_corpus(tmp_path)
    result = mod.search("17 * 23", skip_ripgrep=True)
    paths = {m["path"].replace(os.sep, "/") for m in result["data"]["matches"]}
    assert any("pkg/core.py" in p for p in paths)


def test_regex_query_uses_pattern_not_substring(mod, tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("aaa\nabb\nacc\n", encoding="utf-8")
    result = mod.search("a.+c", regex=True, skip_ripgrep=True)
    lines = [m["line"] for m in result["data"]["matches"]]
    assert lines == ["acc"]


def test_query_with_regex_metacharacters_is_treated_literally_by_default(
    mod, tmp_path: Path
) -> None:
    (tmp_path / "x.py").write_text("a + b = c\n2 + 2 = 4\n", encoding="utf-8")
    result = mod.search("2 + 2", skip_ripgrep=True)
    lines = [m["line"] for m in result["data"]["matches"]]
    assert lines == ["2 + 2 = 4"]


def test_rejects_empty_query(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.SearchFilesError):
        mod.search("", skip_ripgrep=True)


def test_rejects_invalid_limit(mod, tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(mod.SearchFilesError):
        mod.search("x", limit=0, skip_ripgrep=True)
    with pytest.raises(mod.SearchFilesError):
        mod.search("x", limit="big", skip_ripgrep=True)


def test_invalid_regex_raises_search_error(mod, tmp_path: Path) -> None:
    with pytest.raises(mod.SearchFilesError):
        mod.search("[unterminated", regex=True, skip_ripgrep=True)


def test_skips_binary_files(mod, tmp_path: Path) -> None:
    _seed_corpus(tmp_path)
    result = mod.search("binary", skip_ripgrep=True)
    paths = {m["path"].replace(os.sep, "/") for m in result["data"]["matches"]}
    assert all("data.bin" not in p for p in paths)


def test_skips_common_ignored_directories(mod, tmp_path: Path) -> None:
    for d in (".git", "node_modules", "__pycache__"):
        folder = tmp_path / d
        folder.mkdir(parents=True)
        (folder / "leak.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("needle here\n", encoding="utf-8")
    result = mod.search("needle", skip_ripgrep=True)
    paths = {m["path"].replace(os.sep, "/") for m in result["data"]["matches"]}
    assert paths == {"main.py"}


def test_rg_json_parsing(mod) -> None:
    rg_output = (
        '{"type":"summary","data":{"elapsed_total":{"secs":0,"nanos":1000}}}\n'
        '{"type":"match","data":{"path":{"text":"a.py"},"lines":{"text":"return 17 * 23\\n"},'
        '"line_number":2,"submatches":[{"start":7,"end":14}]}}\n'
        '{"type":"end"}\n'
    )
    matches = mod._parse_rg_json(rg_output, limit=10)
    assert len(matches) == 1
    assert matches[0]["path"] == "a.py"
    assert matches[0]["line_number"] == 2
    assert matches[0]["line"] == "return 17 * 23"
    assert matches[0]["match_start"] == 7
    assert matches[0]["match_end"] == 14


def test_rg_path_used_when_available(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_corpus(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> Any:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=(
            '{"type":"match","data":{"path":{"text":"a.py"},'
            '"lines":{"text":"return 17 * 23\\n"},'
            '"line_number":2,"submatches":[{"start":7,"end":14}]}}\n'
        ))

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/fake/rg")
    result = mod.search("17 * 23", runner=fake_runner, skip_ripgrep=False)
    assert result["ok"] is True
    assert len(result["data"]["matches"]) == 1
    assert calls, "rg should have been invoked"
    assert calls[0][0] == "/fake/rg"


def test_rg_unavailable_falls_back_to_python(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_corpus(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    result = mod.search("calculate")
    assert result["ok"] is True
    paths = {m["path"].replace(os.sep, "/") for m in result["data"]["matches"]}
    assert "a.py" in paths
