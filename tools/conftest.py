"""Conftest for tool tests: ensure pytest-asyncio runs async test functions.

Tool tests live under ``tools/`` and are run via the agent-runtime's pytest
(``testpaths = ["tests", "../../tools"]``). Because the test paths resolve
outside ``apps/agent-runtime/``, pytest's rootdir lands at the repo root, and
the runtime's ``asyncio_mode = "auto"`` is not applied. This conftest forces
asyncio auto-mode for the tool tests so ``async def test_...`` works without
per-test markers.
"""


def pytest_configure(config):
    """Enable asyncio auto-mode if the plugin is available."""
    try:
        config.pluginmanager.import_plugin("asyncio")
        config.option.asyncio_mode = "auto"
    except Exception:
        pass
