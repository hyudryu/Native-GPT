"""Load explicitly enabled tools from isolated repository folders."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any

TOOL_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def repo_root() -> Path:
    configured = os.environ.get("AGENTGPT_REPO_ROOT")
    if configured:
        return Path(configured).resolve()
    for candidate in (Path.cwd(), *Path(__file__).resolve().parents):
        if (candidate / "Cargo.toml").is_file() and (candidate / "tools").is_dir():
            return candidate
    return Path.cwd().resolve()


def _tool_dir(tool_id: str, tools_root: Path) -> Path:
    if not TOOL_ID.fullmatch(tool_id):
        raise ValueError(f"invalid tool id: {tool_id}")
    tool_dir = (tools_root / tool_id).resolve()
    if tools_root not in tool_dir.parents:
        raise ValueError(f"invalid tool id: {tool_id}")
    return tool_dir


def load_tool_manifests(tool_ids: list[str], root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read `manifest.json` metadata for each tool id (no code execution).

    Used by the chat runner to learn which enabled tools require approval.
    A missing/unreadable manifest maps to `{}` so callers fail closed.
    """
    root = (root or repo_root()).resolve()
    tools_root = (root / "tools").resolve()
    manifests: dict[str, dict[str, Any]] = {}
    for tool_id in tool_ids:
        manifest_path = _tool_dir(tool_id, tools_root) / "manifest.json"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        manifests[tool_id] = data if isinstance(data, dict) else {}
    return manifests


def load_tools(tool_ids: list[str], root: Path | None = None) -> list[Any]:
    """Import only validated tool IDs selected by the trusted Rust host.

    A tool folder's `TOOL` export may be a single Strands tool or a
    list/tuple of tools (multi-tool folders like `todo-list`); lists are
    flattened into the returned list. Every loaded tool object is tagged
    with its source folder id in `agentgpt_tool_id` so approval gating can
    still map flattened tools back to their manifest.
    """

    root = (root or repo_root()).resolve()
    tools_root = (root / "tools").resolve()
    loaded: list[Any] = []
    for tool_id in tool_ids:
        module_path = _tool_dir(tool_id, tools_root) / "tool.py"
        if not module_path.is_file():
            raise ValueError(f"tool not found: {tool_id}")
        spec = importlib.util.spec_from_file_location(
            f"agentgpt_local_tool_{tool_id.replace('-', '_')}", module_path
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"tool could not be loaded: {tool_id}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tool = getattr(module, "TOOL", None)
        if tool is None:
            raise ValueError(f"tool {tool_id} must export TOOL")
        tools = list(tool) if isinstance(tool, (list, tuple)) else [tool]
        if not tools:
            raise ValueError(f"tool {tool_id} exports an empty TOOL list")
        for item in tools:
            try:
                item.agentgpt_tool_id = tool_id
            except (AttributeError, TypeError):
                pass  # exotic tool objects without attribute support
            loaded.append(item)
    return loaded
