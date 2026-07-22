"""Helper for tool tests: load a tool module in isolation.

Tool tests need to import `tools/<id>/tool.py`, which does `from strands import
tool` at module load time. Without isolation that pollutes `sys.modules` and
breaks sibling tests in the same session that assert `strands` is not imported
at startup (notably `apps/agent-runtime/tests/test_protocol.py`).

Usage:

    from testloader import tool_module

    @pytest.fixture()
    def mod():
        m = tool_module(__file__, "web_fetch_tool")
        yield m
        mod.cleanup()  # strips strands + the tool module from sys.modules

Or, more concisely, use the autouse-ready `isolated_tool_module` fixture
factory.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_tool_module(test_file: str, module_name: str, tool_relpath: str = "tool.py") -> ModuleType:
    """Import the `tool.py` sitting next to `test_file` as `module_name`.

    Returns the loaded module. Call `cleanup_tool_module(module_name)` in your
    fixture's teardown to drop it (and strands) from `sys.modules`.
    """
    tool_dir = Path(test_file).resolve().parent
    spec = importlib.util.spec_from_file_location(module_name, tool_dir / tool_relpath)
    assert spec is not None and spec.loader is not None, f"could not load spec for {tool_dir / tool_relpath}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cleanup_tool_module(*module_names: str) -> None:
    """Remove the named tool module(s) and any strands import from sys.modules."""
    keys_to_drop = set(module_names)
    for key in list(sys.modules.keys()):
        if key in keys_to_drop or key == "strands" or key.startswith("strands."):
            sys.modules.pop(key, None)
