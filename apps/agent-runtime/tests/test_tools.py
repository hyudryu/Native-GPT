from __future__ import annotations

from pathlib import Path

import pytest

from agentgpt_runtime.tools.registry import load_tool_manifests, load_tools


def test_loader_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    with pytest.raises(ValueError, match="invalid tool id"):
        load_tools(["../outside"], tmp_path)


def test_loader_imports_only_requested_tool(tmp_path: Path) -> None:
    folder = tmp_path / "tools" / "example"
    folder.mkdir(parents=True)
    (folder / "tool.py").write_text("TOOL = 'loaded'\n", encoding="utf-8")
    assert load_tools(["example"], tmp_path) == ["loaded"]


def test_loader_flattens_multi_tool_folders(tmp_path: Path) -> None:
    """A folder exporting TOOL as a list contributes every tool, in order."""
    folder = tmp_path / "tools" / "multi"
    folder.mkdir(parents=True)
    (folder / "tool.py").write_text("TOOL = ['one', 'two']\n", encoding="utf-8")
    assert load_tools(["multi"], tmp_path) == ["one", "two"]


def test_loader_rejects_empty_tool_list(tmp_path: Path) -> None:
    folder = tmp_path / "tools" / "empty"
    folder.mkdir(parents=True)
    (folder / "tool.py").write_text("TOOL = []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty TOOL list"):
        load_tools(["empty"], tmp_path)


def test_loader_tags_tools_with_source_folder_id(tmp_path: Path) -> None:
    """Flattened tools carry `agentgpt_tool_id` for manifest-based gating."""
    folder = tmp_path / "tools" / "tagged"
    folder.mkdir(parents=True)
    (folder / "tool.py").write_text(
        "class _T:\n    pass\nTOOL = [_T(), _T()]\n", encoding="utf-8"
    )
    tools = load_tools(["tagged"], tmp_path)
    assert len(tools) == 2
    assert all(t.agentgpt_tool_id == "tagged" for t in tools)


def test_load_tool_manifests_reads_metadata(tmp_path: Path) -> None:
    folder = tmp_path / "tools" / "gated"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        '{"requires_approval": true, "risk": "execute"}', encoding="utf-8"
    )
    # A tool without a manifest maps to {} — the caller decides the default.
    (tmp_path / "tools" / "bare").mkdir()
    manifests = load_tool_manifests(["gated", "bare"], tmp_path)
    assert manifests["gated"]["requires_approval"] is True
    assert manifests["gated"]["risk"] == "execute"
    assert manifests["bare"] == {}


def test_load_tool_manifests_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    with pytest.raises(ValueError, match="invalid tool id"):
        load_tool_manifests(["../outside"], tmp_path)
