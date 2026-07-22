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
