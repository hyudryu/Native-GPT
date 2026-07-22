"""Load explicitly enabled tools from isolated repository folders."""

from __future__ import annotations

import importlib.util
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


def load_tools(tool_ids: list[str], root: Path | None = None) -> list[Any]:
    """Import only validated tool IDs selected by the trusted Rust host."""

    root = (root or repo_root()).resolve()
    tools_root = (root / "tools").resolve()
    loaded: list[Any] = []
    for tool_id in tool_ids:
        if not TOOL_ID.fullmatch(tool_id):
            raise ValueError(f"invalid tool id: {tool_id}")
        module_path = (tools_root / tool_id / "tool.py").resolve()
        if tools_root not in module_path.parents or not module_path.is_file():
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
        loaded.append(tool)
    return loaded
