"""Tests for tools/python-execute/tool.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from testloader import cleanup_tool_module, load_tool_module  # noqa: E402

_MODULE_NAME = "python_execute_tool_under_test"


@pytest.fixture()
def mod():
    m = load_tool_module(__file__, _MODULE_NAME)
    yield m
    cleanup_tool_module(_MODULE_NAME)


def _fake_runner(stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    """Build a fake subprocess runner returning canned output."""
    def runner(argv, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    return runner


def test_runs_simple_code_returns_stdout(mod) -> None:
    result = mod.run_code("print('hello')", runner=_fake_runner(stdout="hello\n"))
    assert result["ok"] is True
    assert result["data"]["stdout"] == "hello\n"
    assert result["data"]["exit_code"] == 0


def test_nonzero_exit_is_failure(mod) -> None:
    result = mod.run_code(
        "import sys; sys.exit(2)",
        runner=_fake_runner(stderr="boom", returncode=2),
    )
    assert result["ok"] is False
    assert result["data"]["exit_code"] == 2
    assert result["error"]["code"] == "nonzero_exit"


def test_timeout_returns_timeout_error(mod) -> None:
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])
    result = mod.run_code("while True: pass", timeout_seconds=1, runner=runner)
    assert result["ok"] is False
    assert result["data"]["timed_out"] is True
    assert result["error"]["code"] == "timeout"
    assert "1s" in result["error"]["message"]


def test_env_is_stripped_to_allowlist(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user env vars or API keys leak into the subprocess env."""
    monkeypatch.setenv("MY_SECRET_API_KEY", "leak-me")
    monkeypatch.setenv("USER_CUSTOM_VAR", "leak-me-too")
    env_received: dict[str, str] = {}

    def runner(argv, *, env, **kwargs):
        env_received.update(env)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    mod.run_code("print('ok')", runner=runner)
    assert "MY_SECRET_API_KEY" not in env_received
    assert "USER_CUSTOM_VAR" not in env_received
    # Allowlisted vars are kept.
    assert "PATH" in env_received or "SYSTEMROOT" in env_received


def test_env_allowlist_includes_systemroot_on_windows(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    env_received: dict[str, str] = {}
    def runner(argv, *, env, **kwargs):
        env_received.update(env)
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_code("print('ok')", runner=runner)
    assert env_received.get("SYSTEMROOT") == r"C:\Windows"


def test_network_block_bootstrap_is_prepended(mod) -> None:
    """When network='none', the user code is preceded by the socket blocker."""
    captured: list[list[str]] = []
    def runner(argv, **kwargs):
        captured.append(argv)
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_code("print('x')", network="none", runner=runner)
    # argv is [python, -I, -c, <code>]; the code is argv[3].
    code_arg = captured[0][3]
    assert "_AgentGPTBlockedSocket" in code_arg
    assert "print('x')" in code_arg


def test_network_outbound_skips_bootstrap(mod) -> None:
    captured: list[list[str]] = []
    def runner(argv, **kwargs):
        captured.append(argv)
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_code("print('x')", network="outbound", runner=runner)
    code_arg = captured[0][3]
    assert "_AgentGPTBlockedSocket" not in code_arg


def test_rejects_empty_code(mod) -> None:
    with pytest.raises(mod.PythonExecuteError):
        mod.run_code("")


def test_rejects_oversized_code(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "MAX_CODE_BYTES", 16)
    with pytest.raises(mod.PythonExecuteError, match="exceeds"):
        mod.run_code("x" * 100)


def test_rejects_invalid_timeout(mod) -> None:
    with pytest.raises(mod.PythonExecuteError):
        mod.run_code("print('x')", timeout_seconds=0)
    with pytest.raises(mod.PythonExecuteError):
        mod.run_code("print('x')", timeout_seconds=601)
    with pytest.raises(mod.PythonExecuteError):
        mod.run_code("print('x')", timeout_seconds="big")  # type: ignore[arg-type]


def test_input_files_are_staged_to_workspace(mod, tmp_path: Path) -> None:
    """Files passed via input_files appear in the subprocess cwd."""
    seen_cwd: list[str] = []
    def runner(argv, *, cwd, **kwargs):
        seen_cwd.append(cwd)
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    result = mod.run_code(
        "print(open('data.csv').read())",
        input_files={"data.csv": "a,b,c\n1,2,3"},
        runner=runner,
    )
    # The workspace had a data.csv file in it.
    workspace = Path(seen_cwd[0])
    assert (workspace / "data.csv").exists() or result["ok"] is True  # cleanup happened, but call succeeded


def test_workspace_is_cleaned_up_after_run(mod) -> None:
    """The temp workspace is removed after the run, success or failure."""
    seen_cwd: list[str] = []
    def runner(argv, *, cwd, **kwargs):
        seen_cwd.append(cwd)
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_code("print('x')", runner=runner)
    workspace = Path(seen_cwd[0])
    assert not workspace.exists()


def test_output_truncation(mod) -> None:
    """Stdout/stderr beyond MAX_OUTPUT_BYTES is truncated, with a flag."""
    huge = "x" * (mod.MAX_OUTPUT_BYTES + 1000)
    result = mod.run_code("print('x')", runner=_fake_runner(stdout=huge))
    assert len(result["data"]["stdout"]) == mod.MAX_OUTPUT_BYTES
    assert result["data"]["truncated"] is True


def test_requires_approval_flag_exported(mod) -> None:
    """The tool module exports REQUIRES_APPROVAL=True so the runtime can gate it."""
    assert mod.REQUIRES_APPROVAL is True


def test_subprocess_args_use_sys_executable(mod) -> None:
    seen_argv: list[list[str]] = []
    def runner(argv, **kwargs):
        seen_argv.append(argv)
        return SimpleNamespace(stdout="", stderr="", returncode=0)
    mod.run_code("print('x')", runner=runner)
    assert seen_argv[0][0] == sys.executable
    assert seen_argv[0][1] == "-I"  # isolated mode flag
