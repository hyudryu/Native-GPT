from __future__ import annotations

from pathlib import Path

import pytest

from agentgpt_runtime.tools.registry import load_tools


def test_loader_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    with pytest.raises(ValueError, match="invalid tool id"):
        load_tools(["../outside"], tmp_path)


def test_loader_imports_only_requested_tool(tmp_path: Path) -> None:
    folder = tmp_path / "tools" / "example"
    folder.mkdir(parents=True)
    (folder / "tool.py").write_text("TOOL = 'loaded'\n", encoding="utf-8")
    assert load_tools(["example"], tmp_path) == ["loaded"]
